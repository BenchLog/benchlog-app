"""Data-access helpers for Project."""

import uuid

from slugify import slugify
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.models import (
    FileVersion,
    JournalEntry,
    Project,
    ProjectFile,
    ProjectLink,
    RelationType,
    User,
)


async def unique_slug(db: AsyncSession, user_id: uuid.UUID, title: str) -> str:
    """Return a slug derived from `title` not yet used by this user.

    Slugs are unique per `(user_id, slug)` — another user having the same
    slug doesn't collide. Collisions within the user's own namespace get a
    `-N` suffix until free.
    """
    base = slugify(title) or "project"
    candidate = base
    counter = 1
    while True:
        existing = await db.execute(
            select(Project.id).where(
                Project.user_id == user_id, Project.slug == candidate
            )
        )
        if existing.scalar_one_or_none() is None:
            return candidate
        counter += 1
        candidate = f"{base}-{counter}"


def normalize_slug(raw: str) -> str:
    """Canonicalize user-supplied slug input via slugify.

    Returns "" for inputs that contain no usable characters (empty, symbols
    only, whitespace only). Callers decide whether empty is allowed.
    """
    return slugify(raw or "")


async def is_slug_taken(
    db: AsyncSession,
    user_id: uuid.UUID,
    slug: str,
    *,
    exclude_project_id: uuid.UUID | None = None,
) -> bool:
    """Check whether `slug` is already used by `user_id`.

    `exclude_project_id` lets the edit flow skip the project being updated
    so saving without changing the slug isn't flagged as a self-collision.
    """
    query = select(Project.id).where(
        Project.user_id == user_id, Project.slug == slug
    )
    if exclude_project_id is not None:
        query = query.where(Project.id != exclude_project_id)
    result = await db.execute(query)
    return result.scalar_one_or_none() is not None


async def get_user_project_by_slug(
    db: AsyncSession, user_id: uuid.UUID, slug: str
) -> Project | None:
    """Owner-scoped lookup — for edit/delete. Returns None for anyone else's project."""
    result = await db.execute(
        select(Project).where(Project.slug == slug, Project.user_id == user_id)
    )
    return result.scalar_one_or_none()


class ForkError(ValueError):
    """Raised by `fork_project` for validation failures.

    Routes catch this and turn it into a 404 (owner-scoped gate) or 400
    (source is private). Mirrors `RelationError` from
    `benchlog.project_relations`.
    """


async def fork_project(
    db: AsyncSession,
    source_project: Project,
    actor_user: User,
) -> Project:
    """Fork `source_project` into a new project owned by `actor_user`.

    Hard copy semantics:
    - Descriptive fields (title, description, status, cover + crop) copied.
    - Category memberships, tags, journal entries, outbound links copied.
    - Journal entry slugs copied verbatim — they're per-project unique, so
      the source's slug is always free in the new project's namespace.
    - Every `ProjectFile` + every `FileVersion` copied, including the
      physical blob (via `benchlog.files.copy_blob`). Thumbnails come
      along for the ride for image versions.

    Explicitly NOT copied:
    - incoming/outgoing inter-project relations (beyond the auto-inserted
      `fork_of` edge described below)
    - collection memberships
    - `pinned`
    - `is_pinned` on journal entries — a fresh fork starts with no pins
    - `is_public` — forks default to PRIVATE regardless of source
    - slug stays the same, deduped inside the actor's namespace

    Ancestry:
    - `forked_from_id = source.id`, `is_fork = True`.

    Side effects:
    - Inserts a system-type `fork_of` relation from new → source.
    - Does NOT commit — caller owns the transaction boundary, matching
      `add_relation` / `toggle_project_in_collection`.

    Validation:
    - `actor_user` cannot own `source_project` (ForkError: self-fork).
    - `source_project.is_public` must be True (ForkError: not forkable).

    Requires `source_project` to have `tags`, `categories`,
    `journal_entries`, `links`, and `files` (with `versions` +
    `current_version`) eager-loaded — the `raise_on_sql` relationships on
    `Project` would otherwise trip.
    """
    # Local imports to sidestep a circular chain (files imports projects'
    # siblings; project_relations would pull us into auth).
    from benchlog.files import copy_blob
    from benchlog.project_relations import add_relation
    from benchlog.storage import get_storage

    if source_project.user_id == actor_user.id:
        raise ForkError("You can't fork your own project.")
    if not source_project.is_public:
        raise ForkError("Only public projects can be forked.")

    # Re-fetch the source with every relationship this helper touches
    # eager-loaded. The detail-route loader doesn't bring `files.versions`
    # along (the detail view only needs `current_version`), so relying on
    # the caller's instance would trip `raise_on_sql` here.
    result = await db.execute(
        select(Project)
        .options(
            selectinload(Project.tags),
            selectinload(Project.categories),
            selectinload(Project.journal_entries),
            selectinload(Project.links),
            selectinload(Project.files).selectinload(ProjectFile.versions),
            selectinload(Project.files).selectinload(ProjectFile.current_version),
        )
        .where(Project.id == source_project.id)
    )
    source_project = result.scalar_one()

    new_slug = await unique_slug_from_slug(
        db, actor_user.id, source_project.slug
    )

    new_project = Project(
        user_id=actor_user.id,
        title=source_project.title,
        slug=new_slug,
        description=source_project.description,
        status=source_project.status,
        pinned=False,
        is_public=False,
        is_fork=True,
        forked_from_id=source_project.id,
        cover_crop_x=source_project.cover_crop_x,
        cover_crop_y=source_project.cover_crop_y,
        cover_crop_width=source_project.cover_crop_width,
        cover_crop_height=source_project.cover_crop_height,
    )
    # Initialize raise_on_sql collections so assignments don't trip the
    # guard. `cover_file_id` is patched in after we copy files and can
    # resolve the source's cover to its copy.
    new_project.tags = list(source_project.tags)
    new_project.categories = list(source_project.categories)
    db.add(new_project)
    # Flush so `new_project.id` is available for child FKs.
    await db.flush()

    # ---- journal entries ----
    # Slugs are per-project unique, so the source's slug is always free in
    # the fork's namespace — copy verbatim without reslugifying. Pin state
    # resets so the fork starts with a clean feed ordering.
    for entry in source_project.journal_entries:
        db.add(
            JournalEntry(
                project_id=new_project.id,
                title=entry.title,
                slug=entry.slug,
                content=entry.content,
                is_public=entry.is_public,
                is_pinned=False,
            )
        )

    # ---- outbound links ----
    for link in source_project.links:
        db.add(
            ProjectLink(
                project_id=new_project.id,
                title=link.title,
                url=link.url,
                link_type=link.link_type,
                sort_order=link.sort_order,
            )
        )

    # ---- files + versions + physical blobs ----
    storage = get_storage()
    # Track source-file-id → new ProjectFile so we can remap the cover
    # pointer once the copies exist.
    file_id_map: dict[uuid.UUID, ProjectFile] = {}
    for src_file in source_project.files:
        new_file = ProjectFile(
            project_id=new_project.id,
            path=src_file.path,
            filename=src_file.filename,
            description=src_file.description,
            show_in_gallery=src_file.show_in_gallery,
        )
        db.add(new_file)
        await db.flush()
        file_id_map[src_file.id] = new_file

        # Copy every version. The source may have versions without a
        # current_version pointer (historical rows) — keep ordering by
        # version_number so the new file's current_version matches the
        # source's current_version. Storage layout is
        # files/<file_id>/<version_number>, so a fresh file_id gives us
        # fresh blob paths without collision.
        new_current_id: uuid.UUID | None = None
        for src_version in sorted(
            src_file.versions, key=lambda v: v.version_number
        ):
            # Copy the blob, not the path — source deletion must not break forks.
            new_storage_path = f"files/{new_file.id}/{src_version.version_number}"
            await copy_blob(storage, src_version.storage_path, new_storage_path)
            new_thumb_path: str | None = None
            if src_version.thumbnail_path:
                new_thumb_path = (
                    f"thumbnails/{new_file.id}/{src_version.version_number}.webp"
                )
                await copy_blob(
                    storage, src_version.thumbnail_path, new_thumb_path
                )
            new_version = FileVersion(
                file_id=new_file.id,
                version_number=src_version.version_number,
                storage_path=new_storage_path,
                original_name=src_version.original_name,
                size_bytes=src_version.size_bytes,
                mime_type=src_version.mime_type,
                checksum=src_version.checksum,
                changelog=src_version.changelog,
                width=src_version.width,
                height=src_version.height,
                thumbnail_path=new_thumb_path,
            )
            db.add(new_version)
            await db.flush()
            if (
                src_file.current_version_id is not None
                and src_version.id == src_file.current_version_id
            ):
                new_current_id = new_version.id
        new_file.current_version_id = new_current_id

    # Remap cover_file_id onto the copy.
    if source_project.cover_file_id is not None:
        copy = file_id_map.get(source_project.cover_file_id)
        if copy is not None:
            new_project.cover_file_id = copy.id
    await db.flush()

    # System-type `fork_of` relation so the chip appears on both sides.
    # Private source would already have been rejected above, so the
    # visibility check inside add_relation is satisfied.
    await add_relation(
        db,
        new_project,
        source_project.id,
        RelationType.fork_of,
        actor_user,
        allow_system_types=True,
    )

    return new_project


async def unique_slug_from_slug(
    db: AsyncSession, user_id: uuid.UUID, slug: str
) -> str:
    """Return a slug derived from an existing slug not yet used by `user_id`.

    Like `unique_slug` but skips the title→slugify step — we already have
    a canonical slug from the source project and want to reuse it verbatim
    when the actor's namespace is free.
    """
    base = slug or "project"
    candidate = base
    counter = 1
    while True:
        existing = await db.execute(
            select(Project.id).where(
                Project.user_id == user_id, Project.slug == candidate
            )
        )
        if existing.scalar_one_or_none() is None:
            return candidate
        counter += 1
        candidate = f"{base}-{counter}"


async def get_project_by_username_and_slug(
    db: AsyncSession, username: str, slug: str
) -> Project | None:
    """Look up the canonical `/u/{username}/{slug}` view target.

    Username matching is case-insensitive so `/u/Alice/foo` and
    `/u/alice/foo` land on the same project. Tags, owner, journal
    entries, links, and files (with current versions) are eager-loaded
    so the detail template doesn't trip the `raise_on_sql` guard.
    """
    from benchlog.models import ProjectFile  # local to avoid circular import

    result = await db.execute(
        select(Project)
        .options(
            selectinload(Project.user),
            selectinload(Project.tags),
            selectinload(Project.categories),
            selectinload(Project.journal_entries),
            selectinload(Project.links),
            selectinload(Project.files).selectinload(ProjectFile.current_version),
            selectinload(Project.cover_file).selectinload(ProjectFile.current_version),
            # Fork parent: the header renders "Forked from @user/slug" when
            # present and needs the owner's username to build the link.
            selectinload(Project.forked_from).selectinload(Project.user),
        )
        .join(Project.user)
        .where(
            func.lower(User.username) == username.lower(),
            Project.slug == slug,
        )
    )
    return result.scalar_one_or_none()
