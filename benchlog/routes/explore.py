from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.activity import ACTIVITY_PAGE_SIZE, list_global_activity
from benchlog.categories import get_categories_flat, get_descendants_map
from benchlog.database import get_db
from benchlog.dependencies import current_user
from benchlog.models import Project, ProjectFile, User
from benchlog.routes.projects import (
    STATUS_LABELS,
    STATUS_VALUES,
    _apply_filter_query,
    _apply_search_query,
    _clean_category_list,
    _clean_category_mode,
    _clean_q,
    _clean_status_list,
    _clean_tag_list,
    _clean_tag_mode,
    _status_options,
    _tsquery_for,
)
from benchlog.tags import get_public_tag_slugs
from benchlog.templating import templates

router = APIRouter()


@router.get("/explore/activity")
async def explore_activity(
    request: Request,
    offset: int = 0,
    user: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Global firehose of public-project activity, paginated by offset."""
    offset = max(offset, 0)
    events = await list_global_activity(
        db,
        viewer_id=user.id if user is not None else None,
        limit=ACTIVITY_PAGE_SIZE,
        offset=offset,
    )
    has_more = len(events) == ACTIVITY_PAGE_SIZE
    return templates.TemplateResponse(
        request,
        "explore/activity.html",
        {
            "user": user,
            "events": events,
            "offset": offset,
            "next_offset": offset + ACTIVITY_PAGE_SIZE if has_more else None,
        },
    )


@router.get("/explore")
async def explore(
    request: Request,
    status: Annotated[list[str] | None, Query()] = None,
    tag: Annotated[list[str] | None, Query()] = None,
    tag_mode: Annotated[str | None, Query()] = None,
    category: Annotated[list[str] | None, Query()] = None,
    category_mode: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    user: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    current_statuses = _clean_status_list(status)
    current_tags = _clean_tag_list(tag)
    current_tag_mode = _clean_tag_mode(tag_mode)
    current_categories = _clean_category_list(category)
    current_category_mode = _clean_category_mode(category_mode)
    current_q = _clean_q(q)

    query = (
        select(Project)
        .options(
            selectinload(Project.user),
            selectinload(Project.tags),
            selectinload(Project.categories),
            selectinload(Project.cover_file).selectinload(ProjectFile.current_version),
        )
        .where(Project.is_public.is_(True))
    )
    descendants_map = (
        await get_descendants_map(db) if current_categories else None
    )
    query = _apply_filter_query(
        query,
        statuses=current_statuses,
        tags=current_tags,
        tag_mode=current_tag_mode,
        categories=current_categories,
        category_mode=current_category_mode,
        category_descendants_map=descendants_map,
    )
    query = _apply_search_query(query, q=current_q)
    # With a query, sort by relevance (ts_rank_cd) and fall back to recency
    # for ties. Without a query, keep the legacy recency-only ordering —
    # Explore has no pinned sort.
    search_ts = _tsquery_for(current_q) if current_q else None
    if search_ts is not None:
        query = query.order_by(
            func.ts_rank_cd(Project.search_vector, search_ts).desc(),
            Project.updated_at.desc(),
        )
    else:
        query = query.order_by(Project.updated_at.desc())

    result = await db.execute(query)
    projects = list(result.scalars().unique().all())

    known_tags = await get_public_tag_slugs(db)
    # Categories are shared across all projects so the whole taxonomy
    # shows up in the filter regardless of which categories currently
    # appear on public projects. An empty-result category is a fine
    # answer — "no public projects in this bucket yet."
    category_options = await get_categories_flat(db)
    # Avoid a circular import by reusing the helper directly.
    from benchlog.routes.projects import _breadcrumbs_for
    all_cats = [c for p in projects for c in p.categories]
    category_breadcrumbs = await _breadcrumbs_for(db, all_cats)

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
            "current_categories": current_categories,
            "current_category_mode": current_category_mode,
            # Explore hardcodes visibility=public; expose it so the filter
            # partial can render a stable context without branching.
            "current_visibility": "all",
            "current_q": current_q,
            "known_tags": known_tags,
            "category_options": category_options,
            "category_breadcrumbs": category_breadcrumbs,
            "status_options": _status_options(),
            "status_labels": STATUS_LABELS,
            "base_url": "/explore",
            "is_explore": True,
            "show_visibility": False,
            "tag_href_prefix": "/explore",
            "category_href_prefix": "/explore",
        },
    )
