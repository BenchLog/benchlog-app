"""Data-access helpers for User profile views."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.models import (
    Project,
    ProjectFile,
    ProjectStatus,
    User,
)


async def get_active_user_by_username(
    db: AsyncSession, username: str
) -> User | None:
    """Case-insensitive lookup. Returns None for missing OR inactive users.

    Collapsing "no such user" and "deactivated user" to a single None lets
    the profile route render a single 404 for both — avoids exposing the
    existence of a disabled account to guests.

    `social_links` is eager-loaded because the profile template renders it,
    and `User.social_links` is `lazy='raise_on_sql'`.
    """
    result = await db.execute(
        select(User)
        .options(selectinload(User.social_links))
        .where(
            func.lower(User.username) == username.lower(),
            User.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def get_public_projects_for_user(
    db: AsyncSession, user_id: uuid.UUID, *, limit: int = 50
) -> list[Project]:
    """Public projects for a user ordered pinned-first, then most-recent.

    Tags + cover file (with current version) are eager-loaded so
    `project_card.html` doesn't trip the `raise_on_sql` guard. Archived
    projects are excluded — they're public-but-closed and the profile is
    meant to read like a live portfolio.
    """
    result = await db.execute(
        select(Project)
        .options(
            selectinload(Project.user),
            selectinload(Project.tags),
            selectinload(Project.categories),
            selectinload(Project.cover_file).selectinload(ProjectFile.current_version),
        )
        .where(
            Project.user_id == user_id,
            Project.is_public.is_(True),
            Project.status != ProjectStatus.archived,
        )
        .order_by(Project.pinned.desc(), Project.updated_at.desc())
        .limit(limit)
    )
    return list(result.scalars().unique().all())
