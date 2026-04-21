"""Routes for Collections — user-curated named groups of their own projects.

URL scheme:
- `/u/{username}/collections` — list page (owner: all; guest: public-only)
- `/u/{username}/collections/new` — create form (owner)
- `POST /u/{username}/collections` — create (owner)
- `/u/{username}/collections/{slug}` — detail page
- `/u/{username}/collections/{slug}/edit` — edit form (owner)
- `POST /u/{username}/collections/{slug}` — update (owner)
- `POST /u/{username}/collections/{slug}/delete` — delete (owner)
- `POST /u/{username}/collections/{slug}/projects` — toggle a project's
  membership (owner, AJAX JSON: {project_id, action: "add"|"remove"})

Route ordering: literal segments (`new`) before `{slug}` parameter. Same
gotcha as projects/files.
"""

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.collections import (
    get_collection_by_username_and_slug,
    get_user_collection_by_slug,
    is_collection_slug_taken,
    list_user_collections_with_counts,
    toggle_project_in_collection,
    unique_collection_slug,
)
from benchlog.database import get_db
from benchlog.dependencies import current_user, require_user
from benchlog.models import Collection, Project, User
from benchlog.projects import normalize_slug
from benchlog.templating import templates
from benchlog.users import get_active_user_by_username

router = APIRouter()


# ---------- form rendering ---------- #


def _empty_form_values() -> dict:
    return {"name": "", "slug": "", "description": "", "is_public": False}


def _form_values_from_collection(collection: Collection) -> dict:
    return {
        "name": collection.name,
        "slug": collection.slug,
        "description": collection.description or "",
        "is_public": collection.is_public,
    }


def _form_values_from_submission(
    *, name: str, slug: str, description: str, is_public: str | None
) -> dict:
    return {
        "name": name,
        "slug": slug,
        "description": description,
        "is_public": bool(is_public),
    }


async def _render_form(
    request: Request,
    user: User,
    *,
    collection: Collection | None,
    form_values: dict,
    error: str | None,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request,
        "collections/form.html",
        {
            "user": user,
            "collection": collection,
            "form_values": form_values,
            "error": error,
        },
        status_code=status_code,
    )


def _visible_projects(collection: Collection, is_owner: bool):
    """Filter to only public projects for non-owner viewers.

    A public collection can reference private projects — the collection
    owner decides to keep it grouped; viewers who aren't the owner just
    don't see the private ones. Owners always see their full list.
    """
    if is_owner:
        return list(collection.projects)
    return [p for p in collection.projects if p.is_public]


# ---------- list ---------- #


@router.get("/u/{username}/collections")
async def list_collections(
    username: str,
    request: Request,
    viewer: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    profile_user = await get_active_user_by_username(db, username)
    if profile_user is None:
        raise HTTPException(status_code=404)
    is_owner = viewer is not None and viewer.id == profile_user.id
    rows = await list_user_collections_with_counts(
        db, profile_user.id, public_only=not is_owner
    )
    return templates.TemplateResponse(
        request,
        "collections/list.html",
        {
            "user": viewer,
            "profile_user": profile_user,
            "is_owner": is_owner,
            "collection_rows": rows,
        },
    )


# ---------- create ---------- #


@router.get("/u/{username}/collections/new")
async def new_collection_form(
    username: str,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)
    return await _render_form(
        request,
        user,
        collection=None,
        form_values=_empty_form_values(),
        error=None,
    )


@router.post("/u/{username}/collections")
async def create_collection(
    username: str,
    request: Request,
    name: str = Form(""),
    slug: str = Form(""),
    description: str = Form(""),
    is_public: str | None = Form(None),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)

    values = _form_values_from_submission(
        name=name.strip(),
        slug=slug.strip(),
        description=description,
        is_public=is_public,
    )
    # JSON mode is used by the project-detail add-to-collections combobox
    # to create + add in one flow. Full form still works unchanged.
    json_mode = "application/json" in request.headers.get("accept", "")

    async def fail(msg: str):
        if json_mode:
            return JSONResponse({"detail": msg}, status_code=400)
        return await _render_form(
            request,
            user,
            collection=None,
            form_values=values,
            error=msg,
            status_code=400,
        )

    if not values["name"]:
        return await fail("Name is required.")

    if values["slug"]:
        normalized = normalize_slug(values["slug"])
        if not normalized:
            return await fail("Slug must contain letters or numbers.")
        if await is_collection_slug_taken(db, user.id, normalized):
            return await fail(
                f"\u201c{normalized}\u201d is already used by another of your collections."
            )
        values["slug"] = normalized
        final_slug = normalized
    else:
        final_slug = await unique_collection_slug(db, user.id, values["name"])

    collection = Collection(
        user_id=user.id,
        name=values["name"],
        slug=final_slug,
        description=values["description"].strip() or None,
        is_public=values["is_public"],
    )
    db.add(collection)
    await db.commit()
    if json_mode:
        return JSONResponse(
            {
                "id": str(collection.id),
                "slug": collection.slug,
                "name": collection.name,
                "is_public": collection.is_public,
            },
            status_code=201,
        )
    return RedirectResponse(
        f"/u/{user.username}/collections/{final_slug}", status_code=302
    )


# ---------- detail ---------- #


@router.get("/u/{username}/collections/{slug}")
async def collection_detail(
    username: str,
    slug: str,
    request: Request,
    viewer: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    collection = await get_collection_by_username_and_slug(db, username, slug)
    if collection is None:
        raise HTTPException(status_code=404)
    is_owner = viewer is not None and collection.user_id == viewer.id
    # Private collections 404 for anyone but the owner — no guest peek,
    # regardless of which projects they might reference.
    if not is_owner and not collection.is_public:
        raise HTTPException(status_code=404)

    visible = _visible_projects(collection, is_owner)
    # Order by updated_at desc — explicit here so guests and owners see
    # the same order; the relationship itself isn't ordered.
    visible.sort(key=lambda p: p.updated_at, reverse=True)

    return templates.TemplateResponse(
        request,
        "collections/detail.html",
        {
            "user": viewer,
            "collection": collection,
            "is_owner": is_owner,
            "visible_projects": visible,
        },
    )


# ---------- edit / update / delete ---------- #


@router.get("/u/{username}/collections/{slug}/edit")
async def edit_collection_form(
    username: str,
    slug: str,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)
    collection = await get_user_collection_by_slug(db, user.id, slug)
    if collection is None:
        raise HTTPException(status_code=404)
    return await _render_form(
        request,
        user,
        collection=collection,
        form_values=_form_values_from_collection(collection),
        error=None,
    )


@router.post("/u/{username}/collections/{slug}")
async def update_collection(
    username: str,
    slug: str,
    request: Request,
    name: str = Form(""),
    new_slug: str = Form("", alias="slug"),
    description: str = Form(""),
    is_public: str | None = Form(None),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)
    collection = await get_user_collection_by_slug(db, user.id, slug)
    if collection is None:
        raise HTTPException(status_code=404)

    values = _form_values_from_submission(
        name=name.strip(),
        slug=new_slug.strip(),
        description=description,
        is_public=is_public,
    )

    async def fail(msg: str):
        return await _render_form(
            request,
            user,
            collection=collection,
            form_values=values,
            error=msg,
            status_code=400,
        )

    if not values["name"]:
        return await fail("Name is required.")
    if not values["slug"]:
        return await fail("Slug is required.")

    normalized = normalize_slug(values["slug"])
    if not normalized:
        return await fail("Slug must contain letters or numbers.")
    values["slug"] = normalized

    if normalized != collection.slug and await is_collection_slug_taken(
        db, user.id, normalized, exclude_id=collection.id
    ):
        return await fail(
            f"\u201c{normalized}\u201d is already used by another of your collections."
        )

    collection.name = values["name"]
    collection.slug = normalized
    collection.description = values["description"].strip() or None
    collection.is_public = values["is_public"]
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/collections/{collection.slug}", status_code=302
    )


@router.post("/u/{username}/collections/{slug}/delete")
async def delete_collection(
    username: str,
    slug: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)
    collection = await get_user_collection_by_slug(db, user.id, slug)
    if collection is None:
        raise HTTPException(status_code=404)
    await db.delete(collection)
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/collections", status_code=302
    )


# ---------- AJAX: toggle project membership ---------- #


@router.post("/u/{username}/collections/{slug}/projects")
async def toggle_project(
    username: str,
    slug: str,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Add or remove a project in this collection.

    Body: JSON ``{"project_id": "<uuid>", "action": "add" | "remove"}``.
    Returns 204 on success; 400 for malformed body / unknown action; 404
    when the target collection or project isn't owned by the caller.
    """
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    action = payload.get("action")
    if action not in {"add", "remove"}:
        raise HTTPException(
            status_code=400, detail="action must be 'add' or 'remove'."
        )
    project_id_raw = payload.get("project_id")
    try:
        project_id = uuid.UUID(str(project_id_raw))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id.")

    # Collection with its `projects` eager-loaded so the toggle helper can
    # diff without triggering a lazy load.
    result = await db.execute(
        select(Collection)
        .options(selectinload(Collection.projects))
        .where(Collection.user_id == user.id, Collection.slug == slug)
    )
    collection = result.scalar_one_or_none()
    if collection is None:
        raise HTTPException(status_code=404)

    # The project must also be owned by the caller — collections hold
    # only the owner's own projects. A cross-user project_id gets 404.
    project = await db.execute(
        select(Project).where(
            Project.id == project_id, Project.user_id == user.id
        )
    )
    project_obj = project.scalar_one_or_none()
    if project_obj is None:
        raise HTTPException(status_code=404)

    changed = await toggle_project_in_collection(
        db, collection, project_obj, on=(action == "add")
    )
    if changed:
        await db.commit()
    return Response(status_code=204)
