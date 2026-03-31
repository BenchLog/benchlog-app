from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.database import get_db
from benchlog.models import Project, Tag
from benchlog.models.tag import ProjectTag
from benchlog.templating import templates

router = APIRouter()


@router.get("/tags", response_class=HTMLResponse)
async def tag_list(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Tag).order_by(Tag.name))
    tags = result.scalars().all()
    return templates.TemplateResponse(request, "tags/list.html", {"tags": tags})


@router.post("/tags", response_class=HTMLResponse)
async def create_tag(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    name = form.get("name", "").strip()
    color = form.get("color", "").strip() or None

    tag = Tag(name=name, slug=slugify(name), color=color)
    db.add(tag)
    await db.commit()

    if request.headers.get("hx-request"):
        result = await db.execute(select(Tag).order_by(Tag.name))
        tags = result.scalars().all()
        return templates.TemplateResponse(request, "tags/_tag_list.html", {"tags": tags})

    return RedirectResponse("/tags", status_code=302)


@router.post("/tags/{tag_id}/edit", response_class=HTMLResponse)
async def edit_tag(request: Request, tag_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Tag).where(Tag.id == tag_id))
    tag = result.scalar_one_or_none()
    if not tag:
        return HTMLResponse("Tag not found", status_code=404)

    form = await request.form()
    tag.name = form.get("name", "").strip()
    tag.slug = slugify(tag.name)
    tag.color = form.get("color", "").strip() or None
    await db.commit()

    if request.headers.get("hx-request"):
        result = await db.execute(select(Tag).order_by(Tag.name))
        tags = result.scalars().all()
        return templates.TemplateResponse(request, "tags/_tag_list.html", {"tags": tags})

    return RedirectResponse("/tags", status_code=302)


@router.post("/tags/{tag_id}/delete", response_class=HTMLResponse)
async def delete_tag(request: Request, tag_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Tag).where(Tag.id == tag_id))
    tag = result.scalar_one_or_none()
    if tag:
        await db.delete(tag)
        await db.commit()

    if request.headers.get("hx-request"):
        result = await db.execute(select(Tag).order_by(Tag.name))
        tags = result.scalars().all()
        return templates.TemplateResponse(request, "tags/_tag_list.html", {"tags": tags})

    return RedirectResponse("/tags", status_code=302)


@router.get("/tags/{slug}", response_class=HTMLResponse)
async def projects_by_tag(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    return RedirectResponse(f"/?tag={slug}", status_code=302)


@router.post("/projects/{slug}/tags", response_class=HTMLResponse)
async def add_tag_to_project(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    tag_id = form.get("tag_id")

    result = await db.execute(
        select(Project).options(selectinload(Project.tags)).where(Project.slug == slug)
    )
    project = result.scalar_one_or_none()
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    tag_result = await db.execute(select(Tag).where(Tag.id == tag_id))
    tag = tag_result.scalar_one_or_none()
    if tag and tag not in project.tags:
        project.tags.append(tag)
        await db.commit()

    return templates.TemplateResponse(request, "components/_project_tags.html", {
        "project": project,
    })


@router.post("/projects/{slug}/tags/{tag_id}/remove", response_class=HTMLResponse)
async def remove_tag_from_project(request: Request, slug: str, tag_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Project).options(selectinload(Project.tags)).where(Project.slug == slug)
    )
    project = result.scalar_one_or_none()
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    project.tags = [t for t in project.tags if str(t.id) != tag_id]
    await db.commit()

    return templates.TemplateResponse(request, "components/_project_tags.html", {
        "project": project,
    })
