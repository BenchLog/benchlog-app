"""Tag parsing, lookup, and project-tag sync helpers."""

import uuid

from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.models import Project, ProjectTag, Tag

# Slug column is String(64); keep room for a safety margin.
MAX_TAG_LENGTH = 64
# Hard cap per project — keeps the project card readable and prevents
# accidental spam. Can be revisited when we see real-world usage.
MAX_TAGS_PER_PROJECT = 12


def parse_tag_input(raw: str) -> list[str]:
    """Split comma/whitespace-separated tag input into unique slugs.

    - Accepts `#foo, bar baz, Qux`
    - Strips leading `#`
    - Slugifies each piece (lowercase, hyphenated)
    - Drops empties and duplicates, preserves first-seen order
    - Truncates to MAX_TAGS_PER_PROJECT
    """
    if not raw:
        return []
    # Commas are primary separators; also split on whitespace runs so
    # space-separated hashtag-style input still works.
    pieces: list[str] = []
    for chunk in raw.split(","):
        pieces.extend(chunk.split())

    seen: set[str] = set()
    slugs: list[str] = []
    for piece in pieces:
        cleaned = piece.lstrip("#")
        slug = slugify(cleaned)[:MAX_TAG_LENGTH]
        if not slug or slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
        if len(slugs) >= MAX_TAGS_PER_PROJECT:
            break
    return slugs


async def get_or_create_tags(
    db: AsyncSession, slugs: list[str]
) -> list[Tag]:
    """Return Tag rows for each slug, creating any that don't exist yet.

    The return order matches the input order so callers can preserve the
    user-typed sequence when rendering chips.
    """
    if not slugs:
        return []

    existing_result = await db.execute(
        select(Tag).where(Tag.slug.in_(slugs))
    )
    existing = {tag.slug: tag for tag in existing_result.scalars()}

    tags: list[Tag] = []
    for slug in slugs:
        tag = existing.get(slug)
        if tag is None:
            tag = Tag(slug=slug)
            db.add(tag)
            existing[slug] = tag  # Avoid duplicate insert within the loop.
        tags.append(tag)

    # Flush so new tag ids are assigned before the association is written.
    await db.flush()
    return tags


async def get_user_tag_slugs(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    """Return distinct tag slugs this user has used, alphabetically sorted.

    Powers the "reuse your tags" chip list under the form's tags input. Scoped
    to the user's own projects rather than the global vocabulary so the list
    stays personal and short.
    """
    result = await db.execute(
        select(Tag.slug)
        .join(ProjectTag, ProjectTag.tag_id == Tag.id)
        .join(Project, Project.id == ProjectTag.project_id)
        .where(Project.user_id == user_id)
        .distinct()
        .order_by(Tag.slug)
    )
    return list(result.scalars().all())


async def set_project_tags(
    db: AsyncSession, project: Project, slugs: list[str]
) -> None:
    """Replace `project.tags` with the tags matching `slugs`.

    Caller is responsible for committing. The relationship must already be
    loaded on `project` (eager-load or fresh instance) so SQLAlchemy can
    diff the collection.
    """
    tags = await get_or_create_tags(db, slugs)
    project.tags = tags
