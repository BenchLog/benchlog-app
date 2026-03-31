import uuid

import markdown as md
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.database import get_db
from benchlog.models import Project
from benchlog.models.update import ProjectUpdate
from benchlog.templating import templates

router = APIRouter()


async def _get_project(slug: str, db: AsyncSession) -> Project | None:
    result = await db.execute(select(Project).where(Project.slug == slug))
    return result.scalar_one_or_none()


@router.get("/projects/{slug}/updates", response_class=HTMLResponse)
async def update_feed(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    result = await db.execute(
        select(ProjectUpdate)
        .where(ProjectUpdate.project_id == project.id)
        .order_by(ProjectUpdate.created_at.desc())
    )
    updates = result.scalars().all()

    # Render markdown for each update
    rendered = []
    for u in updates:
        rendered.append({
            "entry": u,
            "content_html": md.markdown(u.content, extensions=["fenced_code", "tables"]),
        })

    return templates.TemplateResponse(request, "updates/feed.html", {
        "project": project,
        "updates": rendered,
    })


@router.get("/projects/{slug}/updates/new", response_class=HTMLResponse)
async def new_update_form(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    return templates.TemplateResponse(request, "updates/form.html", {
        "project": project,
        "update": None,
    })


@router.post("/projects/{slug}/updates")
async def create_update(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    form = await request.form()
    title = form.get("title", "").strip() or None
    content = form.get("content", "").strip()

    update = ProjectUpdate(
        project_id=project.id,
        title=title,
        content=content,
    )
    db.add(update)
    await db.commit()

    return RedirectResponse(f"/projects/{slug}/updates", status_code=302)


@router.get("/projects/{slug}/updates/{update_id}", response_class=HTMLResponse)
async def single_update(request: Request, slug: str, update_id: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    result = await db.execute(
        select(ProjectUpdate).where(ProjectUpdate.id == uuid.UUID(update_id))
    )
    update = result.scalar_one_or_none()
    if not update:
        return HTMLResponse("Update not found", status_code=404)

    content_html = md.markdown(update.content, extensions=["fenced_code", "tables"])

    return templates.TemplateResponse(request, "updates/detail.html", {
        "project": project,
        "update": update,
        "content_html": content_html,
    })


@router.get("/projects/{slug}/updates/{update_id}/edit", response_class=HTMLResponse)
async def edit_update_form(request: Request, slug: str, update_id: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    result = await db.execute(
        select(ProjectUpdate).where(ProjectUpdate.id == uuid.UUID(update_id))
    )
    update = result.scalar_one_or_none()
    if not update:
        return HTMLResponse("Update not found", status_code=404)

    return templates.TemplateResponse(request, "updates/form.html", {
        "project": project,
        "update": update,
    })


@router.post("/projects/{slug}/updates/{update_id}/edit")
async def edit_update(request: Request, slug: str, update_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ProjectUpdate).where(ProjectUpdate.id == uuid.UUID(update_id))
    )
    update = result.scalar_one_or_none()
    if not update:
        return HTMLResponse("Update not found", status_code=404)

    form = await request.form()
    update.title = form.get("title", "").strip() or None
    update.content = form.get("content", "").strip()
    await db.commit()

    return RedirectResponse(f"/projects/{slug}/updates/{update_id}", status_code=302)


@router.post("/projects/{slug}/updates/{update_id}/delete")
async def delete_update(request: Request, slug: str, update_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ProjectUpdate).where(ProjectUpdate.id == uuid.UUID(update_id))
    )
    update = result.scalar_one_or_none()
    if update:
        await db.delete(update)
        await db.commit()

    return RedirectResponse(f"/projects/{slug}/updates", status_code=302)
