"""Data-access helpers for ProjectUpdate."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.models import ProjectUpdate


async def get_update_by_id(
    db: AsyncSession, project_id: uuid.UUID, update_id: uuid.UUID
) -> ProjectUpdate | None:
    """Fetch a single update scoped to its parent project.

    Always pair the update id with its project so a crafted URL can't pull
    an update out from under its project's visibility rules.
    """
    result = await db.execute(
        select(ProjectUpdate).where(
            ProjectUpdate.id == update_id,
            ProjectUpdate.project_id == project_id,
        )
    )
    return result.scalar_one_or_none()
