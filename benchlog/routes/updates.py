import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.database import get_db
from benchlog.dependencies import current_user, require_user
from benchlog.models import Project, ProjectUpdate, User
from benchlog.projects import (
    get_project_by_username_and_slug,
    get_user_project_by_slug,
)
from benchlog.templating import templates
from benchlog.updates import get_update_by_id

router = APIRouter()


# ---------- visibility helpers ---------- #


def _visible_updates(project: Project, is_owner: bool) -> list[ProjectUpdate]:
    """Return the updates a viewer should see.

    Owners see everything. Everyone else sees only the updates flagged
    public (and only if the project itself is public — but that gate is
    already enforced upstream before we render the feed).
    """
    if is_owner:
        return list(project.updates)
    return [u for u in project.updates if u.is_public]


def _can_view_update(update: ProjectUpdate, is_owner: bool) -> bool:
    """Owner always; else the update must be flagged public."""
    return is_owner or update.is_public


# ---------- owner helpers ---------- #


async def _require_owned_project(
    db: AsyncSession, user: User, username: str, slug: str
):
    """Return the owner's project or 404.

    Mirrors the behaviour used for project edit/update/delete: the URL
    username must match the signed-in user AND the slug must belong to
    them. Anything else is 404.
    """
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)
    project = await get_user_project_by_slug(db, user.id, slug)
    if project is None:
        raise HTTPException(status_code=404)
    return project


# ---------- updates tab ---------- #


@router.get("/u/{username}/{slug}/updates")
async def updates_tab(
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
    if not is_owner and not project.is_public:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "projects/updates.html",
        {
            "user": user,
            "project": project,
            "is_owner": is_owner,
            "updates": _visible_updates(project, is_owner),
        },
    )


# ---------- create ---------- #


@router.get("/u/{username}/{slug}/updates/new")
async def new_update_form(
    username: str,
    slug: str,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    return templates.TemplateResponse(
        request,
        "updates/form.html",
        {
            "user": user,
            "project": project,
            "update": None,
            "form_values": {"title": "", "content": "", "is_public": False},
            "error": None,
        },
    )


@router.post("/u/{username}/{slug}/updates")
async def create_update(
    username: str,
    slug: str,
    request: Request,
    title: str = Form(""),
    content: str = Form(""),
    is_public: str | None = Form(None),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    title = title.strip()
    content = content.strip()
    public_flag = bool(is_public)

    if not content:
        return templates.TemplateResponse(
            request,
            "updates/form.html",
            {
                "user": user,
                "project": project,
                "update": None,
                "form_values": {
                    "title": title,
                    "content": content,
                    "is_public": public_flag,
                },
                "error": "Content is required.",
            },
            status_code=400,
        )

    update = ProjectUpdate(
        project_id=project.id,
        title=title or None,
        content=content,
        is_public=public_flag,
    )
    db.add(update)
    await db.commit()
    # Land on the Updates tab anchored to the new entry, so the post is
    # immediately visible in context instead of buried on the overview.
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/updates#update-{update.id}",
        status_code=302,
    )


# ---------- detail (permalink) ---------- #


@router.get("/u/{username}/{slug}/updates/{update_id}")
async def update_detail(
    username: str,
    slug: str,
    update_id: uuid.UUID,
    request: Request,
    user: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await get_project_by_username_and_slug(db, username, slug)
    if project is None:
        raise HTTPException(status_code=404)
    is_owner = user is not None and project.user_id == user.id
    if not is_owner and not project.is_public:
        raise HTTPException(status_code=404)

    update = await get_update_by_id(db, project.id, update_id)
    if update is None or not _can_view_update(update, is_owner):
        raise HTTPException(status_code=404)

    return templates.TemplateResponse(
        request,
        "updates/detail.html",
        {
            "user": user,
            "project": project,
            "update": update,
            "is_owner": is_owner,
        },
    )


# ---------- edit ---------- #


@router.get("/u/{username}/{slug}/updates/{update_id}/edit")
async def edit_update_form(
    username: str,
    slug: str,
    update_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    update = await get_update_by_id(db, project.id, update_id)
    if update is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "updates/form.html",
        {
            "user": user,
            "project": project,
            "update": update,
            "form_values": {
                "title": update.title or "",
                "content": update.content,
                "is_public": update.is_public,
            },
            "error": None,
        },
    )


@router.post("/u/{username}/{slug}/updates/{update_id}")
async def update_update(
    username: str,
    slug: str,
    update_id: uuid.UUID,
    request: Request,
    title: str = Form(""),
    content: str = Form(""),
    is_public: str | None = Form(None),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    update = await get_update_by_id(db, project.id, update_id)
    if update is None:
        raise HTTPException(status_code=404)

    title = title.strip()
    content = content.strip()
    public_flag = bool(is_public)

    if not content:
        return templates.TemplateResponse(
            request,
            "updates/form.html",
            {
                "user": user,
                "project": project,
                "update": update,
                "form_values": {
                    "title": title,
                    "content": content,
                    "is_public": public_flag,
                },
                "error": "Content is required.",
            },
            status_code=400,
        )

    update.title = title or None
    update.content = content
    update.is_public = public_flag
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/updates/{update.id}",
        status_code=302,
    )


# ---------- delete ---------- #


@router.post("/u/{username}/{slug}/updates/{update_id}/delete")
async def delete_update(
    username: str,
    slug: str,
    update_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    update = await get_update_by_id(db, project.id, update_id)
    if update is None:
        raise HTTPException(status_code=404)
    await db.delete(update)
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/updates", status_code=302
    )
