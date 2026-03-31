import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.database import get_db
from benchlog.models import Project
from benchlog.models.link import LinkType, ProjectLink
from benchlog.templating import templates

router = APIRouter()


async def _get_project(slug: str, db: AsyncSession) -> Project | None:
    result = await db.execute(
        select(Project).options(selectinload(Project.tags)).where(Project.slug == slug)
    )
    return result.scalar_one_or_none()


@router.get("/projects/{slug}/links", response_class=HTMLResponse)
async def link_list(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    result = await db.execute(
        select(ProjectLink)
        .where(ProjectLink.project_id == project.id)
        .order_by(ProjectLink.sort_order, ProjectLink.title)
    )
    links = result.scalars().all()

    return templates.TemplateResponse(request, "links/list.html", {
        "project": project,
        "links": links,
        "link_types": [t.value for t in LinkType],
        "active_tab": "links",
    })


@router.post("/projects/{slug}/links", response_class=HTMLResponse)
async def create_link(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    form = await request.form()
    link = ProjectLink(
        project_id=project.id,
        title=form.get("title", "").strip(),
        url=form.get("url", "").strip(),
        link_type=LinkType(form.get("link_type", "other")),
    )
    db.add(link)
    await db.commit()

    result = await db.execute(
        select(ProjectLink)
        .where(ProjectLink.project_id == project.id)
        .order_by(ProjectLink.sort_order, ProjectLink.title)
    )
    links = result.scalars().all()

    if request.headers.get("hx-request"):
        return templates.TemplateResponse(request, "links/_link_list.html", {
            "project": project,
            "links": links,
            "link_types": [t.value for t in LinkType],
        })
    return RedirectResponse(f"/projects/{slug}/links", status_code=302)


@router.post("/projects/{slug}/links/{link_id}/edit", response_class=HTMLResponse)
async def edit_link(request: Request, slug: str, link_id: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    result = await db.execute(select(ProjectLink).where(ProjectLink.id == uuid.UUID(link_id)))
    link = result.scalar_one_or_none()
    if not link:
        return HTMLResponse("Link not found", status_code=404)

    form = await request.form()
    link.title = form.get("title", "").strip()
    link.url = form.get("url", "").strip()
    link.link_type = LinkType(form.get("link_type", "other"))
    await db.commit()

    result = await db.execute(
        select(ProjectLink)
        .where(ProjectLink.project_id == project.id)
        .order_by(ProjectLink.sort_order, ProjectLink.title)
    )
    links = result.scalars().all()

    if request.headers.get("hx-request"):
        return templates.TemplateResponse(request, "links/_link_list.html", {
            "project": project,
            "links": links,
            "link_types": [t.value for t in LinkType],
        })
    return RedirectResponse(f"/projects/{slug}/links", status_code=302)


@router.post("/projects/{slug}/links/{link_id}/delete", response_class=HTMLResponse)
async def delete_link(request: Request, slug: str, link_id: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    result = await db.execute(select(ProjectLink).where(ProjectLink.id == uuid.UUID(link_id)))
    link = result.scalar_one_or_none()
    if link:
        await db.delete(link)
        await db.commit()

    result = await db.execute(
        select(ProjectLink)
        .where(ProjectLink.project_id == project.id)
        .order_by(ProjectLink.sort_order, ProjectLink.title)
    )
    links = result.scalars().all()

    if request.headers.get("hx-request"):
        return templates.TemplateResponse(request, "links/_link_list.html", {
            "project": project,
            "links": links,
            "link_types": [t.value for t in LinkType],
        })
    return RedirectResponse(f"/projects/{slug}/links", status_code=302)
