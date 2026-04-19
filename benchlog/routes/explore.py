from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.database import get_db
from benchlog.dependencies import current_user
from benchlog.models import Project, ProjectStatus, User
from benchlog.templating import templates

router = APIRouter()


@router.get("/explore")
async def explore(
    request: Request,
    user: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(Project)
        .options(selectinload(Project.user))
        .where(
            Project.is_public.is_(True),
            Project.status != ProjectStatus.archived,
        )
        .order_by(Project.updated_at.desc())
    )
    result = await db.execute(query)
    projects = list(result.scalars().all())
    return templates.TemplateResponse(
        request,
        "explore/list.html",
        {"user": user, "projects": projects},
    )
