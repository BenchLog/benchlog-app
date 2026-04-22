"""Data-access helpers for ProjectRelation.

Typed directed edges between projects. The source-project's owner
declares the edge — the target doesn't opt in. Visibility on the detail
page is a separate concern: a relation might exist in the DB but be
hidden from a specific viewer because the target (or source, for
incoming relations) is private.

The Forks feature will call `add_relation(..., allow_system_types=True)`
internally when a new project is created as a fork, injecting a
`fork_of` edge on the user's behalf.
"""

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.models import (
    USER_PICKABLE_TYPES,
    Project,
    ProjectRelation,
    RelationType,
    User,
)


class RelationError(ValueError):
    """Application-level validation error raised from helpers.

    The routes catch this and turn it into a 400 JSON response. Keeping
    it as a plain exception (rather than an HTTPException) means helpers
    stay framework-agnostic and the tests can assert on error messages
    without a full request cycle.
    """


class DuplicateRelationError(RelationError):
    """Raised when the (source, target, type) triple already exists."""


def visible_to(project: Project, viewer_id: uuid.UUID | None) -> bool:
    """True when `viewer_id` may see `project` in a list/chip.

    Guests (`viewer_id is None`) only see public projects. Owners always
    see their own; everyone else sees public-only. Matches the existing
    detail-route gate verbatim.
    """
    if project.is_public:
        return True
    if viewer_id is None:
        return False
    return project.user_id == viewer_id


def filter_visible(
    relations: list[ProjectRelation],
    attr: str,
    viewer_id: uuid.UUID | None,
) -> list[ProjectRelation]:
    """Filter a relation list by the visibility of its `attr` endpoint.

    Use `attr='target'` for outgoing relations (filter out links to
    private projects the viewer can't see) and `attr='source'` for
    incoming ones. Kept as a plain Python filter because the relation
    lists are small (usually single-digit) and the per-row check is a
    flag-compare plus UUID equality — a second DB round trip to
    pre-filter wouldn't win anything.
    """
    return [r for r in relations if visible_to(getattr(r, attr), viewer_id)]


async def get_outgoing_relations(
    db: AsyncSession, project_id: uuid.UUID
) -> list[ProjectRelation]:
    """Relations where `source_id == project_id`, with target eager-loaded.

    Sorted by relation_type (enum order) then by target title so the
    grouped chip list renders in a stable order regardless of insertion
    sequence. The detail template then groups by type for display.
    """
    result = await db.execute(
        select(ProjectRelation)
        .options(
            selectinload(ProjectRelation.target).selectinload(Project.user),
        )
        .join(Project, ProjectRelation.target_id == Project.id)
        .where(ProjectRelation.source_id == project_id)
        .order_by(ProjectRelation.relation_type, Project.title)
    )
    return list(result.scalars().all())


async def get_incoming_relations(
    db: AsyncSession, project_id: uuid.UUID
) -> list[ProjectRelation]:
    """Relations where `target_id == project_id`, with source eager-loaded."""
    result = await db.execute(
        select(ProjectRelation)
        .options(
            selectinload(ProjectRelation.source).selectinload(Project.user),
        )
        .join(Project, ProjectRelation.source_id == Project.id)
        .where(ProjectRelation.target_id == project_id)
        .order_by(ProjectRelation.relation_type, Project.title)
    )
    return list(result.scalars().all())


async def add_relation(
    db: AsyncSession,
    source_project: Project,
    target_id: uuid.UUID,
    relation_type: RelationType,
    actor_user: User,
    *,
    allow_system_types: bool = False,
) -> ProjectRelation:
    """Create a `(source, target, type)` relation on behalf of `actor_user`.

    Validation (in order):
    - `actor_user` must own `source_project` (404 at the route; raised
      as RelationError here — routes check ownership separately).
    - `source_id != target_id` (400).
    - `relation_type` must be user-pickable unless `allow_system_types`
      is True — the Forks feature sets that flag when writing `fork_of`.
    - Target project must exist and be visible to the actor: their own
      project (any visibility) OR another user's public project. Linking
      to someone else's private project is rejected with 400 — we don't
      even acknowledge that a private project exists.
    - Duplicate triple → DuplicateRelationError.

    Caller owns the commit.
    """
    if source_project.user_id != actor_user.id:
        raise RelationError("You don't own this project.")

    if target_id == source_project.id:
        raise RelationError("A project can't relate to itself.")

    if not allow_system_types and relation_type not in USER_PICKABLE_TYPES:
        raise RelationError("That relation type isn't available.")

    result = await db.execute(
        select(Project).where(Project.id == target_id)
    )
    target = result.scalar_one_or_none()
    # Private-target rejection uses the same visibility rule as the
    # detail page: owner always; otherwise only public. Returning the
    # same error for "missing" vs "private other" keeps us from leaking
    # existence of a private project belonging to someone else.
    if target is None or not visible_to(target, actor_user.id):
        raise RelationError("That project can't be linked.")

    relation = ProjectRelation(
        source_id=source_project.id,
        target_id=target_id,
        relation_type=relation_type,
    )
    db.add(relation)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateRelationError(
            "That relation already exists."
        ) from exc
    return relation


async def remove_relation(
    db: AsyncSession, relation_id: uuid.UUID, actor_user: User
) -> bool:
    """Delete a relation; source-owner only. Returns True when removed.

    Matches the owner-gate convention: unknown id, wrong id, or a
    relation owned by someone else all collapse to False at this layer.
    The route turns False into 404. Caller owns the commit.
    """
    result = await db.execute(
        select(ProjectRelation)
        .join(Project, ProjectRelation.source_id == Project.id)
        .where(
            ProjectRelation.id == relation_id,
            Project.user_id == actor_user.id,
        )
    )
    relation = result.scalar_one_or_none()
    if relation is None:
        return False
    await db.delete(relation)
    return True


async def search_linkable_projects(
    db: AsyncSession,
    actor_user: User,
    q: str,
    *,
    exclude_project_id: uuid.UUID,
    limit: int = 15,
) -> list[Project]:
    """Candidate projects for the "Add relation" combobox.

    Returns projects the actor is allowed to link *to*:
    - their own projects (any visibility)
    - other users' public projects

    `exclude_project_id` drops the source itself (the combobox lives on
    its detail page — self-links are rejected anyway, and including it
    in the dropdown would invite the user to try). The `q` filter uses
    the same prefix-tsquery helper as `/projects` and `/explore`; empty
    `q` returns recently-updated candidates instead.

    `user` is eager-loaded so the result rows can render `· @username`
    next to the title. Result cap is small (15) — this is a typeahead
    not a list page.
    """
    # Local import to avoid a circular dependency — routes/projects.py
    # imports from helpers modules, and some helpers import from other
    # route-level utilities.
    from benchlog.routes.projects import _apply_search_query, _tsquery_for

    q = (q or "").strip()

    query = (
        select(Project)
        .options(selectinload(Project.user))
        .where(
            Project.id != exclude_project_id,
            or_(
                Project.user_id == actor_user.id,
                Project.is_public.is_(True),
            ),
        )
    )

    query = _apply_search_query(query, q=q)
    ts = _tsquery_for(q) if q else None
    if ts is not None:
        query = query.order_by(
            func.ts_rank_cd(Project.search_vector, ts).desc(),
            Project.updated_at.desc(),
        )
    else:
        query = query.order_by(Project.updated_at.desc())

    query = query.limit(limit)
    result = await db.execute(query)
    return list(result.scalars().unique().all())
