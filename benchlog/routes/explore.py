from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.database import get_db
from benchlog.dependencies import current_user
from benchlog.models import Project, ProjectFile, User
from benchlog.routes.projects import (
    STATUS_LABELS,
    STATUS_VALUES,
    _apply_filter_query,
    _clean_status_list,
    _clean_tag_list,
    _clean_tag_mode,
    _status_options,
)
from benchlog.tags import get_public_tag_slugs
from benchlog.templating import templates

router = APIRouter()


@router.get("/explore")
async def explore(
    request: Request,
    status: Annotated[list[str] | None, Query()] = None,
    tag: Annotated[list[str] | None, Query()] = None,
    tag_mode: Annotated[str | None, Query()] = None,
    user: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    current_statuses = _clean_status_list(status)
    current_tags = _clean_tag_list(tag)
    current_tag_mode = _clean_tag_mode(tag_mode)

    query = (
        select(Project)
        .options(
            selectinload(Project.user),
            selectinload(Project.tags),
            selectinload(Project.cover_file).selectinload(ProjectFile.current_version),
        )
        .where(Project.is_public.is_(True))
    )
    query = _apply_filter_query(
        query,
        statuses=current_statuses,
        tags=current_tags,
        tag_mode=current_tag_mode,
    )
    query = query.order_by(Project.updated_at.desc())

    result = await db.execute(query)
    projects = list(result.scalars().unique().all())

    known_tags = await get_public_tag_slugs(db)

    return templates.TemplateResponse(
        request,
        "explore/list.html",
        {
            "user": user,
            "projects": projects,
            "statuses": STATUS_VALUES,
            "current_statuses": current_statuses,
            "current_tags": current_tags,
            "current_tag_mode": current_tag_mode,
            # Explore hardcodes visibility=public; expose it so the filter
            # partial can render a stable context without branching.
            "current_visibility": "all",
            "known_tags": known_tags,
            "status_options": _status_options(),
            "status_labels": STATUS_LABELS,
            "base_url": "/explore",
            "is_explore": True,
            "show_visibility": False,
            "tag_href_prefix": "/explore",
        },
    )
