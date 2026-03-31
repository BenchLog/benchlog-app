from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.database import get_db
from benchlog.models import Project, Tag
from benchlog.models.project import ProjectStatus
from benchlog.services import image_service
from benchlog.templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def project_list(request: Request, status: str | None = None, tag: str | None = None, db: AsyncSession = Depends(get_db)):
    query = select(Project).options(selectinload(Project.tags))

    if status:
        query = query.where(Project.status == ProjectStatus(status))
    else:
        query = query.where(Project.status != ProjectStatus.archived)

    if tag:
        query = query.join(Project.tags).where(Tag.slug == tag)

    query = query.order_by(Project.pinned.desc(), Project.updated_at.desc())
    result = await db.execute(query)
    projects = result.scalars().unique().all()

    all_tags_result = await db.execute(select(Tag).order_by(Tag.name))
    all_tags = all_tags_result.scalars().all()

    statuses = [s.value for s in ProjectStatus]

    return templates.TemplateResponse(request, "projects/list.html", {
        "projects": projects,
        "all_tags": all_tags,
        "statuses": statuses,
        "current_status": status,
        "current_tag": tag,
    })


@router.get("/projects/new", response_class=HTMLResponse)
async def new_project_form(request: Request, db: AsyncSession = Depends(get_db)):
    tags_result = await db.execute(select(Tag).order_by(Tag.name))
    return templates.TemplateResponse(request, "projects/form.html", {
        "project": None,
        "all_tags": tags_result.scalars().all(),
        "statuses": [s.value for s in ProjectStatus],
    })


@router.post("/projects")
async def create_project(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    title = form.get("title", "").strip()
    description = form.get("description", "").strip()
    status = form.get("status", "idea")
    tag_ids = form.getlist("tags")

    slug = slugify(title)

    # Ensure unique slug
    existing = await db.execute(select(Project).where(Project.slug == slug))
    if existing.scalar_one_or_none():
        count = 1
        while True:
            candidate = f"{slug}-{count}"
            check = await db.execute(select(Project).where(Project.slug == candidate))
            if not check.scalar_one_or_none():
                slug = candidate
                break
            count += 1

    project = Project(
        title=title,
        slug=slug,
        description=description,
        status=ProjectStatus(status),
        user_id=(await _get_user_id(db)),
    )
    db.add(project)
    await db.flush()

    if tag_ids:
        tags_result = await db.execute(select(Tag).where(Tag.id.in_(tag_ids)))
        project.tags = list(tags_result.scalars().all())

    # Handle cover image upload
    cover = form.get("cover_image")
    if cover and hasattr(cover, "read"):
        cover_data = await cover.read()
        if cover_data:
            image = await image_service.upload_image(
                db, project.user_id, cover_data, cover.filename, project_id=project.id
            )
            project.cover_image_id = image.id

    await db.commit()
    return RedirectResponse(f"/projects/{slug}", status_code=302)


@router.get("/projects/{slug}", response_class=HTMLResponse)
async def project_detail(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Project)
        .options(selectinload(Project.tags))
        .where(Project.slug == slug)
    )
    project = result.scalar_one_or_none()
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    from benchlog.markdown import render_markdown
    description_html = render_markdown(project.description or "")

    return templates.TemplateResponse(request, "projects/detail.html", {
        "project": project,
        "description_html": description_html,
        "active_tab": "overview",
    })


@router.get("/projects/{slug}/edit", response_class=HTMLResponse)
async def edit_project_form(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Project).options(selectinload(Project.tags)).where(Project.slug == slug)
    )
    project = result.scalar_one_or_none()
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    tags_result = await db.execute(select(Tag).order_by(Tag.name))
    return templates.TemplateResponse(request, "projects/form.html", {
        "project": project,
        "all_tags": tags_result.scalars().all(),
        "statuses": [s.value for s in ProjectStatus],
    })


@router.post("/projects/{slug}/edit")
async def update_project(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Project).options(selectinload(Project.tags)).where(Project.slug == slug)
    )
    project = result.scalar_one_or_none()
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    form = await request.form()
    project.title = form.get("title", "").strip()
    project.description = form.get("description", "").strip()
    project.status = ProjectStatus(form.get("status", "idea"))

    new_slug = form.get("slug", "").strip()
    if new_slug and new_slug != project.slug:
        existing = await db.execute(select(Project).where(Project.slug == new_slug))
        if not existing.scalar_one_or_none():
            project.slug = new_slug
            slug = new_slug

    tag_ids = form.getlist("tags")
    if tag_ids:
        tags_result = await db.execute(select(Tag).where(Tag.id.in_(tag_ids)))
        project.tags = list(tags_result.scalars().all())
    else:
        project.tags = []

    # Handle cover image upload
    cover = form.get("cover_image")
    if cover and hasattr(cover, "read"):
        cover_data = await cover.read()
        if cover_data:
            image = await image_service.upload_image(
                db, project.user_id, cover_data, cover.filename, project_id=project.id
            )
            project.cover_image_id = image.id

    await db.commit()
    return RedirectResponse(f"/projects/{slug}", status_code=302)


@router.post("/projects/{slug}/delete")
async def delete_project(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).where(Project.slug == slug))
    project = result.scalar_one_or_none()
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    project.status = ProjectStatus.archived
    await db.commit()
    return RedirectResponse("/", status_code=302)


@router.post("/projects/{slug}/status", response_class=HTMLResponse)
async def toggle_status(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    new_status = form.get("status")

    result = await db.execute(
        select(Project).options(selectinload(Project.tags)).where(Project.slug == slug)
    )
    project = result.scalar_one_or_none()
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    project.status = ProjectStatus(new_status)
    await db.commit()

    return templates.TemplateResponse(request, "components/project_card.html", {"project": project})


@router.post("/projects/{slug}/pin", response_class=HTMLResponse)
async def toggle_pin(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Project).options(selectinload(Project.tags)).where(Project.slug == slug)
    )
    project = result.scalar_one_or_none()
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    project.pinned = not project.pinned
    await db.commit()

    return templates.TemplateResponse(request, "components/project_card.html", {"project": project})


async def _get_user_id(db: AsyncSession):
    """Get the single user's ID (Phase 1 single-user)."""
    from benchlog.models import User
    result = await db.execute(select(User).limit(1))
    user = result.scalar_one_or_none()
    if user:
        return user.id

    # Auto-create the default user on first project creation
    import bcrypt as _bcrypt
    from benchlog.config import settings
    user = User(
        username=settings.username,
        display_name=settings.username,
        password_hash=_bcrypt.hashpw(settings.password.encode(), _bcrypt.gensalt()).decode(),
    )
    db.add(user)
    await db.flush()
    return user.id
