import re
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.database import get_db
from benchlog.dependencies import current_user, require_user
from benchlog.markdown import render as render_markdown
from benchlog.models import Project, ProjectFile, ProjectStatus, ProjectTag, Tag, User
from benchlog.projects import (
    get_project_by_username_and_slug,
    get_user_project_by_slug,
    is_slug_taken,
    normalize_slug,
    unique_slug,
)
from benchlog.tags import get_user_tag_slugs, parse_tag_input, set_project_tags
from benchlog.templating import templates

router = APIRouter()


STATUS_VALUES = [s.value for s in ProjectStatus]

STATUS_LABELS = {
    ProjectStatus.idea.value: "Idea",
    ProjectStatus.in_progress.value: "In progress",
    ProjectStatus.completed.value: "Completed",
    ProjectStatus.archived.value: "Archived",
}

VISIBILITY_CHOICES = {"all", "public", "private"}


def _parse_status(raw: str | None) -> ProjectStatus | None:
    if not raw:
        return None
    try:
        return ProjectStatus(raw)
    except ValueError:
        return None


def _clean_status_list(raw: list[str] | None) -> list[str]:
    """Filter a raw ?status=... list down to known enum values (lowercased)."""
    if not raw:
        return []
    # Preserve user-visible order, drop duplicates & unknowns silently.
    seen: set[str] = set()
    out: list[str] = []
    for value in raw:
        if not value:
            continue
        lowered = value.lower()
        if lowered not in STATUS_VALUES or lowered in seen:
            continue
        seen.add(lowered)
        out.append(lowered)
    return out


def _clean_tag_list(raw: list[str] | None) -> list[str]:
    """Normalize + dedupe a raw ?tag=... list; drop blanks."""
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for value in raw:
        slug = normalize_slug(value or "")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
    return out


def _clean_visibility(raw: str | None) -> str:
    """Coerce ?visibility= to one of {'all','public','private'}. Unknown → 'all'."""
    if raw in VISIBILITY_CHOICES:
        return raw
    return "all"


def _status_options() -> list[tuple[str, str]]:
    return [(value, STATUS_LABELS[value]) for value in STATUS_VALUES]


def _clean_tag_mode(raw: str | None) -> str:
    """Normalize the tag-match mode. Default "all" preserves legacy behaviour
    when callers don't pass anything."""
    return "any" if raw == "any" else "all"


def _clean_q(raw: str | None) -> str:
    """Normalize a raw ?q= value. Whitespace-only / missing → empty string
    (treated as "no search" by `_apply_search_query`)."""
    return (raw or "").strip()


# Strip anything that isn't a word char (letters/digits/underscore) or space.
# Catches tsquery operators (&, |, !, :, *, (, ), <->) so user input can't
# craft query syntax, and also normalizes punctuation to spaces so "flow-ire"
# splits into ["flow", "ire"] for prefix matching.
_QUERY_TOKEN_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _build_prefix_tsquery(q: str) -> str:
    """Turn a free-text search into a tsquery string with prefix matching
    per token. "flowi router" → "flowi:* & router:*". Drops tokens shorter
    than 2 chars — single-letter prefixes scan too much of the GIN index
    and the results are rarely what the user wants."""
    if not q:
        return ""
    cleaned = _QUERY_TOKEN_RE.sub(" ", q)
    tokens = [t for t in cleaned.split() if len(t) >= 2]
    if not tokens:
        return ""
    return " & ".join(f"{t}:*" for t in tokens)


def _tsquery_for(q: str):
    """Build a prefix-matching tsquery expression from a free-text search
    string, or None if the query is empty / all tokens too short. Shared
    between `_apply_search_query` (WHERE) and the routes' ORDER BY so both
    use the exact same parsed query."""
    ts_str = _build_prefix_tsquery(q)
    if not ts_str:
        return None
    return func.to_tsquery("english", ts_str)


def _apply_search_query(query, *, q: str):
    """Filter a Project select by full-text search over title + description.

    Uses prefix matching (per token `:*`) so typing "flowi" finds "Flowire".
    `to_tsquery` is strict about syntax — `_build_prefix_tsquery` sanitizes
    out operators and normalizes whitespace so arbitrary user input can't
    break the query or inject operators.

    Returns `query` unchanged when the search is empty. Caller is
    responsible for overriding their default ORDER BY with ts_rank_cd —
    kept out of here so routes can decide whether relevance or recency
    wins.
    """
    ts = _tsquery_for(q)
    if ts is None:
        return query
    return query.where(Project.search_vector.op("@@")(ts))


def _apply_filter_query(
    query,
    *,
    statuses: list[str],
    tags: list[str],
    tag_mode: str = "all",
):
    """Apply the shared status/tag filters to a Project select.

    - ``statuses`` empty → exclude archived (default list behaviour).
    - ``statuses`` non-empty → keep only those statuses.
    - ``tags`` with ``tag_mode="all"`` (default) → AND: project must carry
      every selected slug. ``tag_mode="any"`` → OR: project must carry at
      least one of the selected slugs (useful for corralling spelling
      variants like "3d-printing" / "3d-printed" on Explore).
    """
    if statuses:
        query = query.where(Project.status.in_(statuses))
    else:
        query = query.where(Project.status != ProjectStatus.archived)

    if tags:
        # Subquery yields project ids matching the tag predicate. Using
        # ProjectTag + Tag keeps us off the `raise_on_sql` association
        # collection on Project.tags.
        matching = (
            select(ProjectTag.project_id)
            .join(Tag, Tag.id == ProjectTag.tag_id)
            .where(Tag.slug.in_(tags))
        )
        if tag_mode == "all":
            matching = matching.group_by(ProjectTag.project_id).having(
                func.count(func.distinct(Tag.slug)) == len(tags)
            )
        # "any" mode: no HAVING — the IN subquery already returns project ids
        # with at least one matching tag.
        query = query.where(Project.id.in_(matching))
    return query


def _empty_form_values() -> dict:
    return {
        "title": "",
        "slug": "",
        "description": "",
        "status": ProjectStatus.idea.value,
        "pinned": False,
        "is_public": False,
        "tags": "",
    }


def _form_values_from_project(project: Project) -> dict:
    return {
        "title": project.title,
        "slug": project.slug,
        "description": project.description or "",
        "status": project.status.value,
        "pinned": project.pinned,
        "is_public": project.is_public,
        "tags": ", ".join(tag.slug for tag in project.tags),
    }


def _form_values_from_submission(
    *,
    title: str,
    slug: str,
    description: str,
    status: str,
    pinned: str | None,
    is_public: str | None,
    tags: str,
) -> dict:
    return {
        "title": title,
        "slug": slug,
        "description": description,
        "status": status if status in STATUS_VALUES else ProjectStatus.idea.value,
        "pinned": bool(pinned),
        "is_public": bool(is_public),
        "tags": tags,
    }


async def _render_form(
    request: Request,
    user: User,
    db: AsyncSession,
    *,
    project: Project | None,
    form_values: dict,
    error: str | None,
    status_code: int = 200,
):
    known_tags = await get_user_tag_slugs(db, user.id)
    return templates.TemplateResponse(
        request,
        "projects/form.html",
        {
            "user": user,
            "project": project,
            "form_values": form_values,
            "statuses": STATUS_VALUES,
            "error": error,
            "known_tags": known_tags,
        },
        status_code=status_code,
    )


# ---------- owner-scoped: my list / create ---------- #


@router.get("/projects")
async def list_projects(
    request: Request,
    status: Annotated[list[str] | None, Query()] = None,
    tag: Annotated[list[str] | None, Query()] = None,
    tag_mode: Annotated[str | None, Query()] = None,
    visibility: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    current_statuses = _clean_status_list(status)
    current_tags = _clean_tag_list(tag)
    current_tag_mode = _clean_tag_mode(tag_mode)
    current_visibility = _clean_visibility(visibility)
    current_q = _clean_q(q)

    query = (
        select(Project)
        .options(
            selectinload(Project.user),
            selectinload(Project.tags),
            selectinload(Project.cover_file).selectinload(ProjectFile.current_version),
        )
        .where(Project.user_id == user.id)
    )
    query = _apply_filter_query(
        query,
        statuses=current_statuses,
        tags=current_tags,
        tag_mode=current_tag_mode,
    )
    query = _apply_search_query(query, q=current_q)
    if current_visibility == "public":
        query = query.where(Project.is_public.is_(True))
    elif current_visibility == "private":
        query = query.where(Project.is_public.is_(False))

    # When a search is active, relevance wins over pinned/recency. Recency as
    # tiebreaker keeps results stable for equally-ranked rows. No `q` → keep
    # the pre-existing pinned-then-recent order so bookmarks don't shuffle.
    search_ts = _tsquery_for(current_q) if current_q else None
    if search_ts is not None:
        query = query.order_by(
            func.ts_rank_cd(Project.search_vector, search_ts).desc(),
            Project.updated_at.desc(),
        )
    else:
        query = query.order_by(Project.pinned.desc(), Project.updated_at.desc())
    result = await db.execute(query)
    projects = list(result.scalars().unique().all())

    known_tags = await get_user_tag_slugs(db, user.id)

    return templates.TemplateResponse(
        request,
        "projects/list.html",
        {
            "user": user,
            "projects": projects,
            "statuses": STATUS_VALUES,
            "current_statuses": current_statuses,
            "current_tags": current_tags,
            "current_tag_mode": current_tag_mode,
            "current_visibility": current_visibility,
            "current_q": current_q,
            "known_tags": known_tags,
            "status_options": _status_options(),
            "status_labels": STATUS_LABELS,
            "base_url": "/projects",
            "is_explore": False,
            "show_visibility": True,
            "tag_href_prefix": "/projects",
        },
    )


@router.get("/projects/new")
async def new_project_form(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    return await _render_form(
        request,
        user,
        db,
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
    tags: str = Form(""),
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
        tags=tags,
    )

    async def fail(msg: str):
        return await _render_form(
            request, user, db, project=None, form_values=values, error=msg, status_code=400
        )

    if not values["title"]:
        return await fail("Title is required.")

    if values["slug"]:
        normalized = normalize_slug(values["slug"])
        if not normalized:
            return await fail("Slug must contain letters or numbers.")
        if await is_slug_taken(db, user.id, normalized):
            return await fail(f"\u201c{normalized}\u201d is already used by another of your projects.")
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
    # Empty list initializes the relationship so set_project_tags can diff it.
    project.tags = []
    db.add(project)
    await set_project_tags(db, project, parse_tag_input(values["tags"]))
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
    # Viewing from a shared context — tag chips link to /explore for discovery.
    return templates.TemplateResponse(
        request,
        "projects/detail.html",
        {
            "user": user,
            "project": project,
            "is_owner": is_owner,
            "tag_href_prefix": "/explore",
        },
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
    result = await db.execute(
        select(Project)
        .options(selectinload(Project.tags))
        .where(Project.slug == slug, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404)
    return await _render_form(
        request,
        user,
        db,
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
    tags: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)
    result = await db.execute(
        select(Project)
        .options(selectinload(Project.tags))
        .where(Project.slug == slug, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404)

    values = _form_values_from_submission(
        title=title.strip(),
        slug=new_slug.strip(),
        description=description,
        status=status,
        pinned=pinned,
        is_public=is_public,
        tags=tags,
    )

    async def fail(msg: str):
        return await _render_form(
            request, user, db, project=project, form_values=values, error=msg, status_code=400
        )

    if not values["title"]:
        return await fail("Title is required.")
    if not values["slug"]:
        return await fail("Slug is required.")

    normalized = normalize_slug(values["slug"])
    if not normalized:
        return await fail("Slug must contain letters or numbers.")
    values["slug"] = normalized

    if normalized != project.slug and await is_slug_taken(
        db, user.id, normalized, exclude_project_id=project.id
    ):
        return await fail(f"\u201c{normalized}\u201d is already used by another of your projects.")

    project.title = values["title"]
    project.slug = normalized
    project.description = values["description"].strip() or None
    project.status = _parse_status(values["status"]) or project.status
    project.pinned = values["pinned"]
    project.is_public = values["is_public"]
    await set_project_tags(db, project, parse_tag_input(values["tags"]))
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}", status_code=302
    )


@router.post("/u/{username}/{slug}/description")
async def update_description(
    username: str,
    slug: str,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Inline description edit — owner-only, accepts JSON or form.

    JSON callers (the detail-page inline editor) get rendered HTML back so
    the client can swap it in without a reload. Form callers (no-JS
    fallback) get redirected to the detail page like the main edit flow.
    """
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)
    project = await get_user_project_by_slug(db, user.id, slug)
    if project is None:
        raise HTTPException(status_code=404)

    content_type = request.headers.get("content-type", "")
    is_json = content_type.startswith("application/json")
    if is_json:
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body.")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON body.")
        description = str(payload.get("description") or "").strip()
    else:
        form = await request.form()
        description = str(form.get("description") or "").strip()

    # Matches the main edit path: empty → NULL; no upper-bound enforced.
    project.description = description or None
    await db.commit()

    if is_json:
        rendered = render_markdown(description) if description else ""
        return JSONResponse({"html": rendered})
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
