"""Routes for the Links tab — sections + link CRUD + reorder + metadata fetch."""

import json
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from benchlog.activity import record_event
from benchlog.config import settings
from benchlog.database import get_db
from benchlog.dependencies import current_user, require_user
from benchlog.link_metadata import fetch_metadata
from benchlog.links import (
    find_section_by_name_key,
    get_link_by_id,
    get_section_by_id,
    next_link_sort_order,
    next_section_sort_order,
    normalize_url,
    section_name_key,
)
from benchlog.models import (
    ActivityEventType,
    LinkSection,
    Project,
    ProjectLink,
    User,
)
from benchlog.projects import (
    get_project_by_username_and_slug,
    get_user_project_by_slug,
)
from benchlog.routes.projects import load_project_header_ctx
from benchlog.templating import templates

router = APIRouter()


# ---------- helpers ---------- #


async def _require_owned_project(
    db: AsyncSession, user: User, username: str, slug: str
) -> Project:
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)
    project = await get_user_project_by_slug(db, user.id, slug)
    if project is None:
        raise HTTPException(status_code=404)
    return project


async def _load_sections(
    db: AsyncSession, project_id: uuid.UUID
) -> list[LinkSection]:
    result = await db.execute(
        select(LinkSection)
        .where(LinkSection.project_id == project_id)
        .options(selectinload(LinkSection.links))
        .order_by(LinkSection.sort_order, LinkSection.created_at)
    )
    return list(result.scalars().all())


async def _metadata_fetcher(url: str) -> dict:
    """Indirection so tests can monkeypatch the network call.

    Production callers pass `fetch_metadata` directly; tests can
    override the module-level name with a stub that returns canned data.
    """
    return await fetch_metadata(
        url, allow_private=settings.metadata_fetch_allow_private
    )


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
    if not is_owner and not project.is_public:
        raise HTTPException(status_code=404)
    sections = await _load_sections(db, project.id)
    if not is_owner:
        # Visitors see only sections that contain at least one link —
        # half-finished scaffolding stays private to the owner.
        sections = [s for s in sections if s.links]
    header_ctx = await load_project_header_ctx(db, user, project)
    return templates.TemplateResponse(
        request,
        "projects/links.html",
        {
            "user": user,
            "project": project,
            "is_owner": is_owner,
            "sections": sections,
            **header_ctx,
        },
    )


# ---------- sections: create ---------- #


@router.post("/u/{username}/{slug}/links/sections")
async def create_section(
    username: str,
    slug: str,
    request: Request,
    name: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)

    cleaned = (name or "").strip()
    if not cleaned:
        return JSONResponse(
            {"detail": "Section name is required."}, status_code=400
        )
    key = section_name_key(cleaned)
    existing = await find_section_by_name_key(db, project.id, key)
    if existing is not None:
        return JSONResponse(
            {"detail": f"A section called '{existing.name}' already exists."},
            status_code=400,
        )

    section = LinkSection(
        project_id=project.id,
        name=cleaned[:120],
        name_key=key[:120],
        sort_order=await next_section_sort_order(db, project.id),
    )
    db.add(section)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return JSONResponse(
            {"detail": "A section with that name already exists."},
            status_code=400,
        )
    await record_event(
        db,
        actor=user,
        project=project,
        event_type=ActivityEventType.link_section_created,
        payload={"section_id": str(section.id), "name": section.name},
    )
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/links", status_code=302
    )


# ---------- sections: reorder (must precede {section_id} routes) ---------- #


@router.post("/u/{username}/{slug}/links/sections/reorder")
async def reorder_sections(
    username: str,
    slug: str,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    form = await request.form()
    raw_ids = form.getlist("section_ids")

    ordered: list[uuid.UUID] = []
    for raw in raw_ids:
        try:
            ordered.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            continue

    result = await db.execute(
        select(LinkSection).where(LinkSection.project_id == project.id)
    )
    by_id = {s.id: s for s in result.scalars().all()}
    for index, section_id in enumerate(ordered):
        section = by_id.get(section_id)
        if section is not None:
            section.sort_order = index
    await db.commit()
    return Response(status_code=204)


# ---------- sections: rename + delete ---------- #


@router.post("/u/{username}/{slug}/links/sections/{section_id}/rename")
async def rename_section(
    username: str,
    slug: str,
    section_id: uuid.UUID,
    name: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    section = await get_section_by_id(db, project.id, section_id)
    if section is None:
        raise HTTPException(status_code=404)

    cleaned = (name or "").strip()
    if not cleaned:
        return JSONResponse(
            {"detail": "Section name is required."}, status_code=400
        )
    key = section_name_key(cleaned)
    if key != section.name_key:
        existing = await find_section_by_name_key(db, project.id, key)
        if existing is not None:
            return JSONResponse(
                {"detail": f"A section called '{existing.name}' already exists."},
                status_code=400,
            )

    old_name = section.name
    section.name = cleaned[:120]
    section.name_key = key[:120]
    await record_event(
        db,
        actor=user,
        project=project,
        event_type=ActivityEventType.link_section_renamed,
        payload={
            "section_id": str(section.id),
            "old_name": old_name,
            "new_name": section.name,
        },
    )
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/links", status_code=302
    )


@router.post("/u/{username}/{slug}/links/sections/{section_id}/delete")
async def delete_section(
    username: str,
    slug: str,
    section_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    section = await get_section_by_id(db, project.id, section_id)
    if section is None:
        raise HTTPException(status_code=404)
    name = section.name
    await db.delete(section)
    await record_event(
        db,
        actor=user,
        project=project,
        event_type=ActivityEventType.link_section_deleted,
        payload={"name": name},
    )
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/links", status_code=302
    )


# ---------- links: create ---------- #


_OG_FIELDS = (
    "og_title",
    "og_description",
    "og_image_url",
    "og_site_name",
    "favicon_url",
)


def _coerce_og(form: dict) -> dict:
    out = {}
    for field in _OG_FIELDS:
        v = (form.get(field) or "").strip() if isinstance(form.get(field), str) else None
        out[field] = v or None
    return out


@router.post("/u/{username}/{slug}/links")
async def create_link(
    username: str,
    slug: str,
    request: Request,
    title: str = Form(""),
    url: str = Form(""),
    section_name: str = Form(""),
    note: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    form = await request.form()
    og_fields = _coerce_og(dict(form))

    title_clean = (title or "").strip()
    note_clean = (note or "").strip() or None
    normalized = normalize_url(url or "")

    def fail(msg: str) -> JSONResponse:
        return JSONResponse({"detail": msg}, status_code=400)

    if not title_clean:
        return fail("Title is required.")
    if not normalized:
        return fail("Please enter a valid URL.")
    if note_clean is not None and len(note_clean) > 280:
        return fail("Note must be 280 characters or fewer.")

    section_clean = (section_name or "").strip()
    if not section_clean:
        return fail("Section is required.")
    key = section_name_key(section_clean)
    section = await find_section_by_name_key(db, project.id, key)
    if section is None:
        section = LinkSection(
            project_id=project.id,
            name=section_clean[:120],
            name_key=key[:120],
            sort_order=await next_section_sort_order(db, project.id),
        )
        db.add(section)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            section = await find_section_by_name_key(db, project.id, key)
            if section is None:
                return fail("Could not create section.")
        else:
            await record_event(
                db,
                actor=user,
                project=project,
                event_type=ActivityEventType.link_section_created,
                payload={"section_id": str(section.id), "name": section.name},
            )

    link = ProjectLink(
        section_id=section.id,
        title=title_clean[:256],
        url=normalized[:2048],
        note=note_clean[:280] if note_clean else None,
        sort_order=await next_link_sort_order(db, section.id),
        **og_fields,
    )
    if any(og_fields.values()):
        from datetime import datetime, timezone
        link.metadata_fetched_at = datetime.now(tz=timezone.utc)
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
            "section_name": section.name,
        },
    )
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/links", status_code=302
    )


# ---------- links: reorder (must precede {link_id} routes) ---------- #


@router.post("/u/{username}/{slug}/links/reorder")
async def reorder_links(
    username: str,
    slug: str,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Take a JSON payload describing the new state and rewrite section_id
    + sort_order on each affected link. Unknown / foreign link IDs and
    section IDs are silently skipped — the client is being cooperative,
    not authoritative.
    """
    project = await _require_owned_project(db, user, username, slug)
    form = await request.form()
    raw = form.get("payload") or ""
    try:
        entries = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        entries = []
    if not isinstance(entries, list):
        entries = []

    sec_result = await db.execute(
        select(LinkSection.id).where(LinkSection.project_id == project.id)
    )
    allowed_sections = {row for (row,) in sec_result.all()}

    link_result = await db.execute(
        select(ProjectLink)
        .join(LinkSection, LinkSection.id == ProjectLink.section_id)
        .where(LinkSection.project_id == project.id)
    )
    by_id = {link.id: link for link in link_result.scalars().all()}

    # Honour the client's `position` field directly so that foreign /
    # skipped entries still consume their slot — keeps the new ordering
    # stable when the cooperative client included rows we filtered out.
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            link_id = uuid.UUID(str(entry.get("link_id")))
            section_id = uuid.UUID(str(entry.get("section_id")))
            position = int(entry.get("position", 0))
        except (ValueError, TypeError):
            continue
        link = by_id.get(link_id)
        if link is None or section_id not in allowed_sections:
            continue
        link.section_id = section_id
        link.sort_order = position

    await db.commit()
    return Response(status_code=204)


# ---------- metadata fetch: must precede {link_id} routes ---------- #


@router.post("/u/{username}/{slug}/links/fetch-metadata")
async def fetch_metadata_endpoint(
    username: str,
    slug: str,
    url: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Modal posts here on URL blur. No persistence — the result is
    rendered into the preview card, then submitted back as hidden form
    fields when the user saves.
    """
    await _require_owned_project(db, user, username, slug)
    if not url:
        return JSONResponse({"detail": "url required"}, status_code=400)
    result = await _metadata_fetcher(url)
    return JSONResponse(result, status_code=200)


# ---------- links: edit (JSON for modal) ---------- #


@router.get("/u/{username}/{slug}/links/{link_id}/edit")
async def edit_link_json(
    username: str,
    slug: str,
    link_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """JSON payload consumed by the edit modal — the full-page form was
    removed in this release."""
    project = await _require_owned_project(db, user, username, slug)
    link = await get_link_by_id(db, project.id, link_id)
    if link is None:
        raise HTTPException(status_code=404)
    section = await get_section_by_id(db, project.id, link.section_id)
    return JSONResponse(
        {
            "id": str(link.id),
            "title": link.title,
            "url": link.url,
            "note": link.note,
            "section_id": str(link.section_id),
            "section_name": section.name if section else "",
            "og_title": link.og_title,
            "og_description": link.og_description,
            "og_image_url": link.og_image_url,
            "og_site_name": link.og_site_name,
            "favicon_url": link.favicon_url,
        }
    )


# ---------- links: update ---------- #


@router.post("/u/{username}/{slug}/links/{link_id}")
async def update_link(
    username: str,
    slug: str,
    link_id: uuid.UUID,
    request: Request,
    title: str = Form(""),
    url: str = Form(""),
    section_name: str = Form(""),
    note: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    link = await get_link_by_id(db, project.id, link_id)
    if link is None:
        raise HTTPException(status_code=404)

    form = await request.form()
    og_fields = _coerce_og(dict(form))

    title_clean = (title or "").strip()
    note_clean = (note or "").strip() or None
    normalized = normalize_url(url or "")

    def fail(msg: str) -> JSONResponse:
        return JSONResponse({"detail": msg}, status_code=400)

    if not title_clean:
        return fail("Title is required.")
    if not normalized:
        return fail("Please enter a valid URL.")
    if note_clean is not None and len(note_clean) > 280:
        return fail("Note must be 280 characters or fewer.")

    section_clean = (section_name or "").strip()
    if not section_clean:
        return fail("Section is required.")
    key = section_name_key(section_clean)
    section = await find_section_by_name_key(db, project.id, key)
    if section is None:
        section = LinkSection(
            project_id=project.id,
            name=section_clean[:120],
            name_key=key[:120],
            sort_order=await next_section_sort_order(db, project.id),
        )
        db.add(section)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            section = await find_section_by_name_key(db, project.id, key)
            if section is None:
                return fail("Could not create section.")
        else:
            await record_event(
                db,
                actor=user,
                project=project,
                event_type=ActivityEventType.link_section_created,
                payload={"section_id": str(section.id), "name": section.name},
            )

    link.title = title_clean[:256]
    link.url = normalized[:2048]
    link.note = (note_clean[:280] if note_clean else None)
    if link.section_id != section.id:
        # Move into the new section at the bottom of its current list.
        link.section_id = section.id
        link.sort_order = await next_link_sort_order(db, section.id)
    for k, v in og_fields.items():
        setattr(link, k, v)
    if any(og_fields.values()):
        from datetime import datetime, timezone
        link.metadata_fetched_at = datetime.now(tz=timezone.utc)

    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/links", status_code=302
    )


# ---------- links: delete ---------- #


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


# ---------- refetch-metadata (per-link) ---------- #


@router.post("/u/{username}/{slug}/links/{link_id}/refetch-metadata")
async def refetch_metadata_endpoint(
    username: str,
    slug: str,
    link_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Edit-modal "Refresh preview" button. Persists the result onto the
    link immediately — diverges from the create-modal flow because the
    user is acting on an already-saved row, not previewing a draft."""
    project = await _require_owned_project(db, user, username, slug)
    link = await get_link_by_id(db, project.id, link_id)
    if link is None:
        raise HTTPException(status_code=404)
    result = await _metadata_fetcher(link.url)
    link.og_title = result.get("title")
    link.og_description = result.get("description")
    link.og_image_url = result.get("image_url")
    link.og_site_name = result.get("site_name")
    link.favicon_url = result.get("favicon_url")
    from datetime import datetime, timezone
    link.metadata_fetched_at = datetime.now(tz=timezone.utc)
    await db.commit()
    return JSONResponse(result, status_code=200)
