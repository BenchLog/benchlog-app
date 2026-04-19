"""Data-access helpers for Project."""

import uuid

from slugify import slugify
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.models import Project, User


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


async def get_project_by_username_and_slug(
    db: AsyncSession, username: str, slug: str
) -> Project | None:
    """Look up the canonical `/u/{username}/{slug}` view target.

    Username matching is case-insensitive so `/u/Alice/foo` and
    `/u/alice/foo` land on the same project. Tags and owner are
    eager-loaded for the detail template.
    """
    result = await db.execute(
        select(Project)
        .options(selectinload(Project.user), selectinload(Project.tags))
        .join(Project.user)
        .where(
            func.lower(User.username) == username.lower(),
            Project.slug == slug,
        )
    )
    return result.scalar_one_or_none()
