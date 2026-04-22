import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.activity import purge_entry_events, record_event
from benchlog.database import get_db
from benchlog.dependencies import current_user, require_user
from benchlog.files import (
    apply_journal_rename_to_project_markdown,
    get_project_entry_index,
    get_project_file_index,
)
from benchlog.journal import (
    can_view_entry,
    get_entry_by_id,
    get_entry_by_slug,
    unique_entry_slug,
    visible_entries,
)
from benchlog.models import ActivityEventType, JournalEntry, Project, User
from benchlog.projects import (
    get_project_by_username_and_slug,
    get_user_project_by_slug,
    normalize_slug,
)
from benchlog.routes.projects import load_project_header_ctx
from benchlog.templating import templates

router = APIRouter()


# ---------- owner helpers ---------- #


async def _require_owned_project(
    db: AsyncSession, user: User, username: str, slug: str
) -> Project:
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


async def _resolve_entry_for_owner(
    db: AsyncSession, project: Project, slug_or_id: str
) -> JournalEntry | None:
    """Look up an entry by slug first, then by UUID.

    Titled entries are addressable by slug (`.../journal/{entry_slug}`);
    untitled entries only by UUID (`.../journal/{entry_id}/edit`). Tries
    slug first so a "happens to parse as a UUID" slug isn't shadowed by
    the UUID fallback.
    """
    entry = await get_entry_by_slug(db, project.id, slug_or_id)
    if entry is not None:
        return entry
    try:
        entry_id = uuid.UUID(slug_or_id)
    except (ValueError, TypeError):
        return None
    return await get_entry_by_id(db, project.id, entry_id)


def _entry_permalink(project: Project, entry: JournalEntry) -> str:
    """Canonical URL for an entry. Titled entries land on their slug
    route; untitled entries fall back to the list (anchored to the entry)
    since they have no deep-link target of their own.
    """
    base = f"/u/{project.user.username}/{project.slug}/journal"
    if entry.slug:
        return f"{base}/{entry.slug}"
    return f"{base}#entry-{entry.id}"


# ---------- journal tab ---------- #


@router.get("/u/{username}/{slug}/journal")
async def journal_tab(
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
    header_ctx = await load_project_header_ctx(db, user, project)
    return templates.TemplateResponse(
        request,
        "projects/journal.html",
        {
            "user": user,
            "project": project,
            "is_owner": is_owner,
            "entries": visible_entries(project.journal_entries, is_owner),
            **header_ctx,
        },
    )


# ---------- create ---------- #
#
# Literal `/new` MUST come before `{entry_slug}` routes to avoid the
# typeahead-slug "new" shadowing the new-entry form.


@router.get("/u/{username}/{slug}/journal/new")
async def new_entry_form(
    username: str,
    slug: str,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    file_index = await get_project_file_index(db, project.id)
    entry_index = await get_project_entry_index(db, project.id)
    return templates.TemplateResponse(
        request,
        "journal/form.html",
        {
            "user": user,
            "project": project,
            "entry": None,
            "form_values": {
                "title": "",
                "slug": "",
                "content": "",
                "is_public": False,
            },
            "error": None,
            "file_index": file_index,
            "entry_index": entry_index,
        },
    )


@router.post("/u/{username}/{slug}/journal")
async def create_entry(
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
        file_index = await get_project_file_index(db, project.id)
        entry_index = await get_project_entry_index(db, project.id)
        return templates.TemplateResponse(
            request,
            "journal/form.html",
            {
                "user": user,
                "project": project,
                "entry": None,
                "form_values": {
                    "title": title,
                    "slug": "",
                    "content": content,
                    "is_public": public_flag,
                },
                "error": "Content is required.",
                "file_index": file_index,
                "entry_index": entry_index,
            },
            status_code=400,
        )

    # Titled entries get a deep-linkable slug; untitled stay inline-only
    # (slug stays NULL, no detail URL).
    entry_slug: str | None = None
    if title:
        entry_slug = await unique_entry_slug(db, project.id, title)

    entry = JournalEntry(
        project_id=project.id,
        title=title or None,
        slug=entry_slug,
        content=content,
        is_public=public_flag,
    )
    db.add(entry)
    await db.flush()
    await record_event(
        db,
        actor=user,
        project=project,
        event_type=ActivityEventType.journal_entry_posted,
        payload={"entry_id": str(entry.id)},
    )
    await db.commit()
    # Land on the Journal tab anchored to the new entry, so the post is
    # immediately visible in context instead of buried on the overview.
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/journal#entry-{entry.id}",
        status_code=302,
    )


# ---------- detail (permalink, titled entries only) ---------- #


@router.get("/u/{username}/{slug}/journal/{entry_slug}")
async def entry_detail(
    username: str,
    slug: str,
    entry_slug: str,
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

    entry = await get_entry_by_slug(db, project.id, entry_slug)
    if entry is None or not can_view_entry(entry, is_owner):
        raise HTTPException(status_code=404)

    header_ctx = await load_project_header_ctx(db, user, project)
    return templates.TemplateResponse(
        request,
        "journal/detail.html",
        {
            "user": user,
            "project": project,
            "entry": entry,
            "is_owner": is_owner,
            **header_ctx,
        },
    )


# ---------- edit ---------- #


@router.get("/u/{username}/{slug}/journal/{entry_ref}/edit")
async def edit_entry_form(
    username: str,
    slug: str,
    entry_ref: str,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    entry = await _resolve_entry_for_owner(db, project, entry_ref)
    if entry is None:
        raise HTTPException(status_code=404)
    file_index = await get_project_file_index(db, project.id)
    entry_index = await get_project_entry_index(db, project.id)
    return templates.TemplateResponse(
        request,
        "journal/form.html",
        {
            "user": user,
            "project": project,
            "entry": entry,
            "form_values": {
                "title": entry.title or "",
                "slug": entry.slug or "",
                "content": entry.content,
                "is_public": entry.is_public,
            },
            "error": None,
            "file_index": file_index,
            "entry_index": entry_index,
        },
    )


@router.post("/u/{username}/{slug}/journal/{entry_ref}")
async def update_entry(
    username: str,
    slug: str,
    entry_ref: str,
    request: Request,
    title: str = Form(""),
    slug_field: str = Form("", alias="slug"),
    content: str = Form(""),
    is_public: str | None = Form(None),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    entry = await _resolve_entry_for_owner(db, project, entry_ref)
    if entry is None:
        raise HTTPException(status_code=404)

    title = title.strip()
    submitted_slug = slug_field.strip()
    content = content.strip()
    public_flag = bool(is_public)

    file_index = None
    entry_index = None

    async def fail(msg: str):
        nonlocal file_index, entry_index
        if file_index is None:
            file_index = await get_project_file_index(db, project.id)
        if entry_index is None:
            entry_index = await get_project_entry_index(db, project.id)
        return templates.TemplateResponse(
            request,
            "journal/form.html",
            {
                "user": user,
                "project": project,
                "entry": entry,
                "form_values": {
                    "title": title,
                    "slug": submitted_slug,
                    "content": content,
                    "is_public": public_flag,
                },
                "error": msg,
                "file_index": file_index,
                "entry_index": entry_index,
            },
            status_code=400,
        )

    if not content:
        return await fail("Content is required.")

    # Sticky-slug rule: a title edit alone doesn't change the slug. The
    # slug only moves when the user explicitly submits a new one, or when
    # the entry transitions between titled and untitled. Mirrors how
    # projects and files treat slug edits.
    old_slug = entry.slug
    old_title = entry.title
    new_slug: str | None = entry.slug
    had_title = entry.title is not None
    has_title = bool(title)

    if not has_title:
        # Untitled entries have no slug and no detail URL.
        new_slug = None
    elif had_title and submitted_slug and submitted_slug != (entry.slug or ""):
        # Explicit slug edit on an already-titled entry.
        normalized = normalize_slug(submitted_slug)
        if not normalized:
            return await fail("Slug must contain letters or numbers.")
        if normalized != entry.slug:
            new_slug = await unique_entry_slug(
                db, project.id, normalized, exclude_id=entry.id
            )
    elif not had_title and has_title:
        # Newly-titled entry — generate a slug from the title (or the
        # submitted slug, if the user typed one on the form).
        source = submitted_slug or title
        new_slug = await unique_entry_slug(db, project.id, source)
    # else: title-only edit on a titled entry — sticky slug, don't touch.

    entry.title = title or None
    entry.slug = new_slug
    entry.content = content
    entry.is_public = public_flag
    await db.commit()

    # Rewrite journal refs across the project's markdown when the slug
    # changes OR the title changes (while the entry stays titled — if it
    # became untitled, new_slug is None and old links intentionally decay
    # to 404s rather than resurface under a fresh auto-slug the author
    # didn't pick). Label rewriting only fires for links whose visible
    # text exactly matches the old title — user-customized labels survive.
    slug_moved = bool(old_slug and new_slug and old_slug != new_slug)
    title_moved = bool(
        old_slug and new_slug and old_title is not None and old_title != title
    )
    if slug_moved or title_moved:
        result = await db.execute(
            select(Project)
            .options(selectinload(Project.journal_entries))
            .where(Project.id == project.id)
        )
        loaded = result.scalar_one_or_none()
        if loaded is not None:
            await apply_journal_rename_to_project_markdown(
                db,
                loaded,
                user.username,
                old_slug,
                new_slug,
                old_title=old_title,
                new_title=title or None,
                skip_entry_id=entry.id,
            )

    return RedirectResponse(_entry_permalink(project, entry), status_code=302)


# ---------- pin / unpin ---------- #


@router.post("/u/{username}/{slug}/journal/{entry_ref}/pin")
async def pin_entry(
    username: str,
    slug: str,
    entry_ref: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    entry = await _resolve_entry_for_owner(db, project, entry_ref)
    if entry is None:
        raise HTTPException(status_code=404)
    entry.is_pinned = True
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/journal", status_code=302
    )


@router.post("/u/{username}/{slug}/journal/{entry_ref}/unpin")
async def unpin_entry(
    username: str,
    slug: str,
    entry_ref: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    entry = await _resolve_entry_for_owner(db, project, entry_ref)
    if entry is None:
        raise HTTPException(status_code=404)
    entry.is_pinned = False
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/journal", status_code=302
    )


# ---------- delete ---------- #


@router.post("/u/{username}/{slug}/journal/{entry_ref}/delete")
async def delete_entry(
    username: str,
    slug: str,
    entry_ref: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    entry = await _resolve_entry_for_owner(db, project, entry_ref)
    if entry is None:
        raise HTTPException(status_code=404)
    entry_id = entry.id
    await db.delete(entry)
    await db.flush()
    await purge_entry_events(db, entry_id)
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/journal", status_code=302
    )
