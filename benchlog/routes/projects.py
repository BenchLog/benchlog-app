from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.database import get_db
from benchlog.dependencies import current_user, require_user
from benchlog.models import Project, ProjectStatus, User
from benchlog.projects import (
    get_project_by_username_and_slug,
    get_user_project_by_slug,
    is_slug_taken,
    normalize_slug,
    unique_slug,
)
from benchlog.templating import templates

router = APIRouter()


STATUS_VALUES = [s.value for s in ProjectStatus]


def _parse_status(raw: str | None) -> ProjectStatus | None:
    if not raw:
        return None
    try:
        return ProjectStatus(raw)
    except ValueError:
        return None


def _empty_form_values() -> dict:
    return {
        "title": "",
        "slug": "",
        "description": "",
        "status": ProjectStatus.idea.value,
        "pinned": False,
        "is_public": False,
    }


def _form_values_from_project(project: Project) -> dict:
    return {
        "title": project.title,
        "slug": project.slug,
        "description": project.description or "",
        "status": project.status.value,
        "pinned": project.pinned,
        "is_public": project.is_public,
    }


def _form_values_from_submission(
    *,
    title: str,
    slug: str,
    description: str,
    status: str,
    pinned: str | None,
    is_public: str | None,
) -> dict:
    return {
        "title": title,
        "slug": slug,
        "description": description,
        "status": status if status in STATUS_VALUES else ProjectStatus.idea.value,
        "pinned": bool(pinned),
        "is_public": bool(is_public),
    }


def _render_form(
    request: Request,
    user: User,
    *,
    project: Project | None,
    form_values: dict,
    error: str | None,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request,
        "projects/form.html",
        {
            "user": user,
            "project": project,
            "form_values": form_values,
            "statuses": STATUS_VALUES,
            "error": error,
        },
        status_code=status_code,
    )


# ---------- owner-scoped: my list / create ---------- #


@router.get("/projects")
async def list_projects(
    request: Request,
    status: str | None = None,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    current_status = _parse_status(status)

    query = (
        select(Project)
        .options(selectinload(Project.user))
        .where(Project.user_id == user.id)
    )
    if current_status is not None:
        query = query.where(Project.status == current_status)
    else:
        query = query.where(Project.status != ProjectStatus.archived)

    query = query.order_by(Project.pinned.desc(), Project.updated_at.desc())
    result = await db.execute(query)
    projects = list(result.scalars().all())

    return templates.TemplateResponse(
        request,
        "projects/list.html",
        {
            "user": user,
            "projects": projects,
            "statuses": STATUS_VALUES,
            "current_status": current_status.value if current_status else None,
        },
    )


@router.get("/projects/new")
async def new_project_form(
    request: Request,
    user: User = Depends(require_user),
):
    return _render_form(
        request,
        user,
        project=None,
        form_values=_empty_form_values(),
        error=request.session.pop("flash_error", None),
    )


@router.post("/projects")
async def create_project(
    request: Request,
    title: str = Form(""),
    slug: str = Form(""),
    description: str = Form(""),
    status: str = Form(ProjectStatus.idea.value),
    pinned: str | None = Form(None),
    is_public: str | None = Form(None),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    values = _form_values_from_submission(
        title=title.strip(),
        slug=slug.strip(),
        description=description,
        status=status,
        pinned=pinned,
        is_public=is_public,
    )

    def fail(msg: str):
        return _render_form(
            request, user, project=None, form_values=values, error=msg, status_code=400
        )

    if not values["title"]:
        return fail("Title is required.")

    if values["slug"]:
        normalized = normalize_slug(values["slug"])
        if not normalized:
            return fail("Slug must contain letters or numbers.")
        if await is_slug_taken(db, user.id, normalized):
            return fail(f"\u201c{normalized}\u201d is already used by another of your projects.")
        values["slug"] = normalized
        final_slug = normalized
    else:
        final_slug = await unique_slug(db, user.id, values["title"])

    project = Project(
        user_id=user.id,
        title=values["title"],
        slug=final_slug,
        description=values["description"].strip() or None,
        status=_parse_status(values["status"]) or ProjectStatus.idea,
        pinned=values["pinned"],
        is_public=values["is_public"],
    )
    db.add(project)
    await db.commit()
    return RedirectResponse(f"/u/{user.username}/{final_slug}", status_code=302)


# ---------- canonical /u/{username}/{slug} surface ---------- #


@router.get("/u/{username}/{slug}")
async def project_detail(
    username: str,
    slug: str,
    request: Request,
    user: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await get_project_by_username_and_slug(db, username, slug)
    if project is None:
        raise HTTPException(status_code=404)
    is_owner = user is not None and project.user_id == user.id
    # Owner sees their own; everyone else (guests included) only sees public.
    if not is_owner and not project.is_public:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "projects/detail.html",
        {"user": user, "project": project, "is_owner": is_owner},
    )


@router.get("/u/{username}/{slug}/edit")
async def edit_project_form(
    username: str,
    slug: str,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)
    project = await get_user_project_by_slug(db, user.id, slug)
    if project is None:
        raise HTTPException(status_code=404)
    return _render_form(
        request,
        user,
        project=project,
        form_values=_form_values_from_project(project),
        error=request.session.pop("flash_error", None),
    )


@router.post("/u/{username}/{slug}")
async def update_project(
    username: str,
    slug: str,
    request: Request,
    title: str = Form(""),
    new_slug: str = Form("", alias="slug"),
    description: str = Form(""),
    status: str = Form(ProjectStatus.idea.value),
    pinned: str | None = Form(None),
    is_public: str | None = Form(None),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)
    project = await get_user_project_by_slug(db, user.id, slug)
    if project is None:
        raise HTTPException(status_code=404)

    values = _form_values_from_submission(
        title=title.strip(),
        slug=new_slug.strip(),
        description=description,
        status=status,
        pinned=pinned,
        is_public=is_public,
    )

    def fail(msg: str):
        return _render_form(
            request, user, project=project, form_values=values, error=msg, status_code=400
        )

    if not values["title"]:
        return fail("Title is required.")
    if not values["slug"]:
        return fail("Slug is required.")

    normalized = normalize_slug(values["slug"])
    if not normalized:
        return fail("Slug must contain letters or numbers.")
    values["slug"] = normalized

    if normalized != project.slug and await is_slug_taken(
        db, user.id, normalized, exclude_project_id=project.id
    ):
        return fail(f"\u201c{normalized}\u201d is already used by another of your projects.")

    project.title = values["title"]
    project.slug = normalized
    project.description = values["description"].strip() or None
    project.status = _parse_status(values["status"]) or project.status
    project.pinned = values["pinned"]
    project.is_public = values["is_public"]
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}", status_code=302
    )


@router.post("/u/{username}/{slug}/delete")
async def delete_project(
    username: str,
    slug: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)
    project = await get_user_project_by_slug(db, user.id, slug)
    if project is None:
        raise HTTPException(status_code=404)
    await db.delete(project)
    await db.commit()
    return RedirectResponse("/projects", status_code=302)
