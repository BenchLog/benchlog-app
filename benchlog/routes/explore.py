from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.database import get_db
from benchlog.dependencies import current_user
from benchlog.models import Project, ProjectFile, ProjectStatus, Tag, User
from benchlog.projects import normalize_slug
from benchlog.templating import templates

router = APIRouter()


@router.get("/explore")
async def explore(
    request: Request,
    tag: str | None = None,
    user: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    current_tag = normalize_slug(tag) if tag else ""

    query = (
        select(Project)
        .options(
            selectinload(Project.user),
            selectinload(Project.tags),
            selectinload(Project.cover_file).selectinload(ProjectFile.current_version),
        )
        .where(
            Project.is_public.is_(True),
            Project.status != ProjectStatus.archived,
        )
    )
    if current_tag:
        query = query.join(Project.tags).where(Tag.slug == current_tag)
    query = query.order_by(Project.updated_at.desc())

    result = await db.execute(query)
    projects = list(result.scalars().unique().all())
    return templates.TemplateResponse(
        request,
        "explore/list.html",
        {
            "user": user,
            "projects": projects,
            "current_tag": current_tag or None,
            "tag_href_prefix": "/explore",
        },
    )
