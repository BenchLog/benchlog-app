"""Data-access helpers for JournalEntry."""

import uuid

from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.models import JournalEntry


async def unique_entry_slug(
    db: AsyncSession,
    project_id: uuid.UUID,
    desired: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> str:
    """Return a slug derived from `desired` not yet used by this project.

    Slugs are unique per `(project_id, slug)` — another project having the
    same slug doesn't collide. Collisions within the project's own namespace
    get a `-N` suffix until free. Mirrors
    `benchlog.collections.unique_collection_slug`.
    """
    base = slugify(desired) or "entry"
    candidate = base
    counter = 1
    while True:
        query = select(JournalEntry.id).where(
            JournalEntry.project_id == project_id,
            JournalEntry.slug == candidate,
        )
        if exclude_id is not None:
            query = query.where(JournalEntry.id != exclude_id)
        existing = await db.execute(query)
        if existing.scalar_one_or_none() is None:
            return candidate
        counter += 1
        candidate = f"{base}-{counter}"


async def get_entry_by_id(
    db: AsyncSession, project_id: uuid.UUID, entry_id: uuid.UUID
) -> JournalEntry | None:
    """Fetch a single journal entry scoped to its parent project.

    Always pair the entry id with its project so a crafted URL can't pull
    an entry out from under its project's visibility rules.
    """
    result = await db.execute(
        select(JournalEntry).where(
            JournalEntry.id == entry_id,
            JournalEntry.project_id == project_id,
        )
    )
    return result.scalar_one_or_none()


async def get_entry_by_slug(
    db: AsyncSession, project_id: uuid.UUID, slug: str
) -> JournalEntry | None:
    """Fetch a titled journal entry by its per-project slug."""
    result = await db.execute(
        select(JournalEntry).where(
            JournalEntry.project_id == project_id,
            JournalEntry.slug == slug,
        )
    )
    return result.scalar_one_or_none()


def visible_entries(entries: list[JournalEntry], is_owner: bool) -> list[JournalEntry]:
    """Return the journal entries a viewer should see.

    Owners see everything. Everyone else sees only the entries flagged
    public (and only if the project itself is public — but that gate is
    already enforced upstream before we render the feed).
    """
    if is_owner:
        return list(entries)
    return [e for e in entries if e.is_public]


def can_view_entry(entry: JournalEntry, is_owner: bool) -> bool:
    """Owner always; else the entry must be flagged public."""
    return is_owner or entry.is_public
