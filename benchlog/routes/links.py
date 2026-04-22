import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.activity import record_event
from benchlog.database import get_db
from benchlog.dependencies import current_user, require_user
from benchlog.links import (
    get_link_by_id,
    next_sort_order,
    normalize_url,
    parse_link_type,
)
from benchlog.models import ActivityEventType, LinkType, ProjectLink, User
from benchlog.projects import (
    get_project_by_username_and_slug,
    get_user_project_by_slug,
)
from benchlog.routes.projects import load_project_header_ctx
from benchlog.templating import templates

router = APIRouter()


LINK_TYPES = list(LinkType)


# ---------- owner helper ---------- #


async def _require_owned_project(
    db: AsyncSession, user: User, username: str, slug: str
):
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)
    project = await get_user_project_by_slug(db, user.id, slug)
    if project is None:
        raise HTTPException(status_code=404)
    return project


def _form_values(
    *, title: str = "", url: str = "", link_type: str = LinkType.other.value
) -> dict:
    return {"title": title, "url": url, "link_type": link_type}


# ---------- links tab ---------- #


@router.get("/u/{username}/{slug}/links")
async def links_tab(
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
    # Links inherit project visibility — there's no per-link flag (unlike
    # journal entries), since a link is part of a project's metadata.
    if not is_owner and not project.is_public:
        raise HTTPException(status_code=404)
    header_ctx = await load_project_header_ctx(db, user, project)
    return templates.TemplateResponse(
        request,
        "projects/links.html",
        {
            "user": user,
            "project": project,
            "is_owner": is_owner,
            **header_ctx,
        },
    )


# ---------- create ---------- #


@router.get("/u/{username}/{slug}/links/new")
async def new_link_form(
    username: str,
    slug: str,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    return templates.TemplateResponse(
        request,
        "links/form.html",
        {
            "user": user,
            "project": project,
            "link": None,
            "form_values": _form_values(),
            "link_types": LINK_TYPES,
            "error": None,
        },
    )


@router.post("/u/{username}/{slug}/links")
async def create_link(
    username: str,
    slug: str,
    request: Request,
    title: str = Form(""),
    url: str = Form(""),
    link_type: str = Form(LinkType.other.value),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)

    values = _form_values(title=title.strip(), url=url.strip(), link_type=link_type)

    def fail(msg: str):
        return templates.TemplateResponse(
            request,
            "links/form.html",
            {
                "user": user,
                "project": project,
                "link": None,
                "form_values": values,
                "link_types": LINK_TYPES,
                "error": msg,
            },
            status_code=400,
        )

    if not values["title"]:
        return fail("Title is required.")
    normalized = normalize_url(values["url"])
    if not normalized:
        return fail("Please enter a valid URL.")
    values["url"] = normalized

    link = ProjectLink(
        project_id=project.id,
        title=values["title"],
        url=normalized,
        link_type=parse_link_type(values["link_type"]),
        # Append at the bottom of any existing arrangement.
        sort_order=await next_sort_order(db, project.id),
    )
    db.add(link)
    await db.flush()
    await record_event(
        db,
        actor=user,
        project=project,
        event_type=ActivityEventType.link_added,
        payload={
            "link_id": str(link.id),
            "label": link.title,
            "url": link.url,
        },
    )
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/links", status_code=302
    )


# ---------- reorder ---------- #
#
# Reorder MUST be declared before the `{link_id}` routes below, otherwise
# FastAPI matches `/links/reorder` as `{link_id}="reorder"` and 422s on
# UUID coercion.


@router.post("/u/{username}/{slug}/links/reorder")
async def reorder_links(
    username: str,
    slug: str,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Accept the new display order as a list of link UUIDs and rewrite
    `sort_order` to match. Unknown IDs (ids that don't belong to this
    project, or were deleted between render and drop) are silently
    skipped — the client is being cooperative, not authoritative.
    """
    project = await _require_owned_project(db, user, username, slug)

    # Parse the form directly — FastAPI's `Form(list[str])` handling is
    # awkward for optional-list defaults, so we pull it off the request.
    form = await request.form()
    raw_ids = form.getlist("link_ids")

    # Coerce to UUIDs; anything non-UUID is ignored rather than 400-ing so
    # a single bad value doesn't wipe the whole order.
    ordered: list[uuid.UUID] = []
    for raw in raw_ids:
        try:
            ordered.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            continue

    result = await db.execute(
        select(ProjectLink).where(ProjectLink.project_id == project.id)
    )
    by_id = {link.id: link for link in result.scalars().all()}

    for index, link_id in enumerate(ordered):
        link = by_id.get(link_id)
        if link is not None:
            link.sort_order = index

    await db.commit()
    # 204: the client already updated the UI optimistically.
    return Response(status_code=204)


# ---------- edit ---------- #


@router.get("/u/{username}/{slug}/links/{link_id}/edit")
async def edit_link_form(
    username: str,
    slug: str,
    link_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    link = await get_link_by_id(db, project.id, link_id)
    if link is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "links/form.html",
        {
            "user": user,
            "project": project,
            "link": link,
            "form_values": _form_values(
                title=link.title, url=link.url, link_type=link.link_type.value
            ),
            "link_types": LINK_TYPES,
            "error": None,
        },
    )


@router.post("/u/{username}/{slug}/links/{link_id}")
async def update_link(
    username: str,
    slug: str,
    link_id: uuid.UUID,
    request: Request,
    title: str = Form(""),
    url: str = Form(""),
    link_type: str = Form(LinkType.other.value),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    link = await get_link_by_id(db, project.id, link_id)
    if link is None:
        raise HTTPException(status_code=404)

    values = _form_values(title=title.strip(), url=url.strip(), link_type=link_type)

    def fail(msg: str):
        return templates.TemplateResponse(
            request,
            "links/form.html",
            {
                "user": user,
                "project": project,
                "link": link,
                "form_values": values,
                "link_types": LINK_TYPES,
                "error": msg,
            },
            status_code=400,
        )

    if not values["title"]:
        return fail("Title is required.")
    normalized = normalize_url(values["url"])
    if not normalized:
        return fail("Please enter a valid URL.")

    link.title = values["title"]
    link.url = normalized
    link.link_type = parse_link_type(values["link_type"])
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/links", status_code=302
    )


# ---------- delete ---------- #


@router.post("/u/{username}/{slug}/links/{link_id}/delete")
async def delete_link(
    username: str,
    slug: str,
    link_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    link = await get_link_by_id(db, project.id, link_id)
    if link is None:
        raise HTTPException(status_code=404)
    label = link.title
    url = link.url
    await db.delete(link)
    await record_event(
        db,
        actor=user,
        project=project,
        event_type=ActivityEventType.link_removed,
        payload={"label": label, "url": url},
    )
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/links", status_code=302
    )
