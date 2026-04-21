"""Data-access helpers for Collection.

Mirrors the shape of `benchlog/projects.py` — per-user slug uniqueness,
owner-scoped lookup, case-insensitive canonical-URL lookup, and a
replace-semantics setter for managing a project's membership.
"""

import uuid

from slugify import slugify
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.models import Collection, CollectionProject, Project, User


async def unique_collection_slug(
    db: AsyncSession,
    user_id: uuid.UUID,
    desired: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> str:
    """Return a slug derived from `desired` not yet used by this user.

    Slugs are unique per `(user_id, slug)` — another user having the same
    slug doesn't collide. Collisions within the user's own namespace get a
    `-N` suffix until free. Matches `benchlog.projects.unique_slug`.
    """
    base = slugify(desired) or "collection"
    candidate = base
    counter = 1
    while True:
        query = select(Collection.id).where(
            Collection.user_id == user_id, Collection.slug == candidate
        )
        if exclude_id is not None:
            query = query.where(Collection.id != exclude_id)
        existing = await db.execute(query)
        if existing.scalar_one_or_none() is None:
            return candidate
        counter += 1
        candidate = f"{base}-{counter}"


async def is_collection_slug_taken(
    db: AsyncSession,
    user_id: uuid.UUID,
    slug: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    """Check whether `slug` is already used by `user_id`."""
    query = select(Collection.id).where(
        Collection.user_id == user_id, Collection.slug == slug
    )
    if exclude_id is not None:
        query = query.where(Collection.id != exclude_id)
    result = await db.execute(query)
    return result.scalar_one_or_none() is not None


async def get_user_collection_by_slug(
    db: AsyncSession, user_id: uuid.UUID, slug: str
) -> Collection | None:
    """Owner-scoped lookup — for edit/delete. Returns None for anyone else."""
    result = await db.execute(
        select(Collection).where(
            Collection.slug == slug, Collection.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def get_collection_by_username_and_slug(
    db: AsyncSession, username: str, slug: str
) -> Collection | None:
    """Case-insensitive username lookup for the canonical detail URL.

    Eager-loads the owner and the membership list with their owners + tags
    + categories + cover files so the detail template can render project
    cards without tripping `raise_on_sql`.
    """
    from benchlog.models import ProjectFile  # local to avoid circular import

    result = await db.execute(
        select(Collection)
        .options(
            selectinload(Collection.user),
            selectinload(Collection.projects).selectinload(Project.user),
            selectinload(Collection.projects).selectinload(Project.tags),
            selectinload(Collection.projects).selectinload(Project.categories),
            selectinload(Collection.projects)
            .selectinload(Project.cover_file)
            .selectinload(ProjectFile.current_version),
        )
        .join(Collection.user)
        .where(
            func.lower(User.username) == username.lower(),
            Collection.slug == slug,
        )
    )
    return result.scalar_one_or_none()


async def list_user_collections(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    public_only: bool = False,
) -> list[Collection]:
    """Owner view → all collections; guest view → public_only=True.

    Project-count metadata is served from a separate helper to keep this
    one light — callers that don't need counts (e.g. the add-to-collections
    modal's checkbox list) pay nothing.
    """
    query = select(Collection).where(Collection.user_id == user_id)
    if public_only:
        query = query.where(Collection.is_public.is_(True))
    query = query.order_by(Collection.updated_at.desc())
    result = await db.execute(query)
    return list(result.scalars().all())


async def list_user_collections_with_counts(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    public_only: bool = False,
) -> list[tuple[Collection, int]]:
    """Same as `list_user_collections` but zipped with total project count.

    Powers the list page — cards show "N projects" as a chip regardless of
    visibility, since the owner wants a quick read on what's inside.
    """
    collections = await list_user_collections(
        db, user_id, public_only=public_only
    )
    if not collections:
        return []
    # One grouped COUNT keeps this cheap even with many collections.
    rows = await db.execute(
        select(
            CollectionProject.collection_id,
            func.count(CollectionProject.project_id),
        )
        .where(
            CollectionProject.collection_id.in_([c.id for c in collections])
        )
        .group_by(CollectionProject.collection_id)
    )
    counts = {cid: n for cid, n in rows.all()}
    return [(c, counts.get(c.id, 0)) for c in collections]


async def get_project_collection_memberships(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> set[uuid.UUID]:
    """Return the set of collection IDs (owned by `user_id`) that contain
    `project_id`. Powers the add-to-collections modal's initial state.
    """
    result = await db.execute(
        select(CollectionProject.collection_id)
        .join(
            Collection,
            Collection.id == CollectionProject.collection_id,
        )
        .where(
            Collection.user_id == user_id,
            CollectionProject.project_id == project_id,
        )
    )
    return {row for row in result.scalars().all()}


def _coerce_uuids(raw_ids: list[str]) -> list[uuid.UUID]:
    """Parse a list of string ids; silently drop anything non-UUID."""
    out: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for value in raw_ids:
        try:
            parsed = uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            continue
        if parsed in seen:
            continue
        seen.add(parsed)
        out.append(parsed)
    return out


async def set_project_collection_membership(
    db: AsyncSession,
    project: Project,
    collection_ids: list[str],
    user_id: uuid.UUID,
) -> None:
    """Replace the project's collection set.

    - UUIDs only; invalid ones dropped silently.
    - Collections not owned by `user_id` dropped silently so a stale form
      submission can't slot the project into someone else's collection.
    - Caller owns the commit.

    Mirrors `set_project_categories` — assigns through the ORM's
    `project.collections` relationship so the unit-of-work syncs
    `collection_projects` cleanly. The relationship is `raise_on_sql`, so
    we preload via `db.refresh` when the collection isn't already hot.
    """
    from sqlalchemy import inspect as sa_inspect

    state = sa_inspect(project)
    if "collections" in state.unloaded:
        await db.refresh(project, ["collections"])

    parsed = _coerce_uuids(collection_ids)
    if not parsed:
        project.collections = []
        return

    # Owner-scope filter lives here (not at the caller) so every callsite
    # gets the guard for free.
    result = await db.execute(
        select(Collection).where(
            Collection.id.in_(parsed), Collection.user_id == user_id
        )
    )
    rows = list(result.scalars().all())
    by_id = {c.id: c for c in rows}
    ordered = [by_id[cid] for cid in parsed if cid in by_id]
    project.collections = ordered


async def toggle_project_in_collection(
    db: AsyncSession,
    collection: Collection,
    project: Project,
    on: bool,
) -> bool:
    """Add or remove a single project membership.

    Returns True when the membership state changed, False when it was
    already the requested value. Caller owns the commit. Collection's
    `projects` collection must be preloaded (both code paths walk it).
    """
    current = {p.id for p in collection.projects}
    if on:
        if project.id in current:
            return False
        collection.projects.append(project)
        return True
    if project.id not in current:
        return False
    collection.projects = [p for p in collection.projects if p.id != project.id]
    return True


async def get_public_collections_for_user(
    db: AsyncSession, user_id: uuid.UUID
) -> list[tuple[Collection, int]]:
    """Public collections surfaced on the profile page, with project counts."""
    return await list_user_collections_with_counts(
        db, user_id, public_only=True
    )
