import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.categories import (
    get_categories_flat,
    set_project_categories,
)
from benchlog.collections import (
    get_project_collection_memberships,
    list_user_collections,
)
from benchlog.database import get_db
from benchlog.dependencies import current_user, require_user
from benchlog.files import get_project_file_index, get_project_file_lookup
from benchlog.markdown import render_for_project
from benchlog.models import (
    Project,
    ProjectCategory,
    ProjectFile,
    ProjectStatus,
    ProjectTag,
    Tag,
    User,
)
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


def _clean_category_list(raw: list[str] | None) -> list[str]:
    """Normalize + dedupe a raw ?category=... list; drop non-UUID values.

    Categories filter by UUID (see benchlog/categories.py for rationale —
    slugs aren't globally unique because of the `(parent_id, slug)` scope).
    Forgiving on bad URLs: silently skip anything unparseable.
    """
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for value in raw:
        if not value:
            continue
        try:
            parsed = uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            continue
        s = str(parsed)
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
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


def _clean_category_mode(raw: str | None) -> str:
    """Same shape as `_clean_tag_mode` but for categories. Default "all"."""
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
    categories: list[str] | None = None,
    category_mode: str = "all",
):
    """Apply the shared status/tag/category filters to a Project select.

    - ``statuses`` empty → exclude archived (default list behaviour).
    - ``statuses`` non-empty → keep only those statuses.
    - ``tags`` with ``tag_mode="all"`` (default) → AND: project must carry
      every selected slug. ``tag_mode="any"`` → OR: project must carry at
      least one of the selected slugs (useful for corralling spelling
      variants like "3d-printing" / "3d-printed" on Explore).
    - ``categories`` with ``category_mode="all"`` (default) → AND: project
      must be assigned to every selected category. ``category_mode="any"``
      → OR: project must be assigned to at least one. Curated taxonomies
      still benefit from the "any" escape — e.g. browsing across siblings
      under the same parent without manually clicking each.
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

    if categories:
        # Same subquery shape as tags. Using ProjectCategory directly
        # avoids touching the raise_on_sql `Project.categories` collection.
        cat_match = (
            select(ProjectCategory.project_id)
            .where(ProjectCategory.category_id.in_(categories))
        )
        if category_mode == "all":
            cat_match = cat_match.group_by(ProjectCategory.project_id).having(
                func.count(func.distinct(ProjectCategory.category_id))
                == len(categories)
            )
        # "any" mode: no HAVING — the IN subquery already returns project
        # ids with at least one matching category.
        query = query.where(Project.id.in_(cat_match))
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
        "categories": [],
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
        "categories": [str(c.id) for c in project.categories],
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
    categories: list[str] | None = None,
) -> dict:
    return {
        "title": title,
        "slug": slug,
        "description": description,
        "status": status if status in STATUS_VALUES else ProjectStatus.idea.value,
        "pinned": bool(pinned),
        "is_public": bool(is_public),
        "tags": tags,
        "categories": _clean_category_list(categories or []),
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
    # Full, admin-curated taxonomy — the picker is shared, not user-scoped.
    known_categories = await get_categories_flat(db)
    # Editors scoped to a project expose a `files/…` typeahead sourced from
    # this index. New projects have no files yet, so send an empty list —
    # the client-side code treats that as "enable typeahead but no matches".
    file_index = (
        await get_project_file_index(db, project.id) if project is not None else []
    )
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
            "known_categories": known_categories,
            "file_index": file_index,
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
    category: Annotated[list[str] | None, Query()] = None,
    category_mode: Annotated[str | None, Query()] = None,
    visibility: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    current_statuses = _clean_status_list(status)
    current_tags = _clean_tag_list(tag)
    current_tag_mode = _clean_tag_mode(tag_mode)
    current_categories = _clean_category_list(category)
    current_category_mode = _clean_category_mode(category_mode)
    current_visibility = _clean_visibility(visibility)
    current_q = _clean_q(q)

    query = (
        select(Project)
        .options(
            selectinload(Project.user),
            selectinload(Project.tags),
            selectinload(Project.categories),
            selectinload(Project.cover_file).selectinload(ProjectFile.current_version),
        )
        .where(Project.user_id == user.id)
    )
    query = _apply_filter_query(
        query,
        statuses=current_statuses,
        tags=current_tags,
        tag_mode=current_tag_mode,
        categories=current_categories,
        category_mode=current_category_mode,
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
    # Flat option list for the filter-bar combobox — breadcrumb-labelled,
    # matches the shape the shared `_category_combobox.html` partial expects.
    category_options = await get_categories_flat(db)
    # Category chip tooltips / detail breadcrumbs all read from one flat
    # {id: "Parent › Child"} dict. Build once and pass to the card partial.
    all_cats = [c for p in projects for c in p.categories]
    category_breadcrumbs = await _breadcrumbs_for(db, all_cats)

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
            "current_categories": current_categories,
            "current_category_mode": current_category_mode,
            "current_visibility": current_visibility,
            "current_q": current_q,
            "known_tags": known_tags,
            "category_options": category_options,
            "category_breadcrumbs": category_breadcrumbs,
            "status_options": _status_options(),
            "status_labels": STATUS_LABELS,
            "base_url": "/projects",
            "is_explore": False,
            "show_visibility": True,
            "tag_href_prefix": "/projects",
            "category_href_prefix": "/projects",
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
    category: list[str] | None = Form(None),
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
        categories=category or [],
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
    # Empty list initializes the relationships so the helpers can assign
    # through them without tripping raise_on_sql lazy loads.
    project.tags = []
    project.categories = []
    db.add(project)
    await set_project_tags(db, project, parse_tag_input(values["tags"]))
    await set_project_categories(db, project, values["categories"])
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
    # File index only matters for the inline description editor (owner-only),
    # so skip the query entirely for guests / non-owners.
    file_index = await get_project_file_index(db, project.id) if is_owner else None
    # Full category breadcrumbs for the header chips — `Parent › Child`
    # labels come from this flat lookup rather than walking `parent`
    # (raise_on_sql guards against that).
    category_breadcrumbs = await _breadcrumbs_for(db, project.categories)
    # Owner-only: pre-hydrate the add-to-collections modal with the full
    # list of the owner's collections + a set of which ones currently
    # contain this project. Passing as context (not a separate fetch)
    # avoids a round-trip on modal open and keeps the button's "N" chip
    # accurate on first paint.
    owner_collections = []
    project_collection_ids: set = set()
    if is_owner:
        owner_collections = await list_user_collections(db, user.id)
        project_collection_ids = await get_project_collection_memberships(
            db, user.id, project.id
        )
    # Viewing from a shared context — tag chips link to /explore for discovery.
    return templates.TemplateResponse(
        request,
        "projects/detail.html",
        {
            "user": user,
            "project": project,
            "is_owner": is_owner,
            "tag_href_prefix": "/explore",
            "category_href_prefix": "/explore",
            "category_breadcrumbs": category_breadcrumbs,
            "file_index": file_index,
            "owner_collections": owner_collections,
            "project_collection_ids": project_collection_ids,
        },
    )


async def _breadcrumbs_for(db: AsyncSession, cats) -> dict[str, str]:
    """Return ``{category_id_str: 'Parent › Child'}`` for the given cats.

    Short-circuits to empty dict when no categories are attached so callers
    don't have to branch. Hits `get_categories_flat` once and filters down.
    """
    if not cats:
        return {}
    flat = await get_categories_flat(db)
    by_id = {str(row["id"]): row["breadcrumb"] for row in flat}
    return {str(c.id): by_id.get(str(c.id), c.name) for c in cats}


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
        .options(
            selectinload(Project.tags),
            selectinload(Project.categories),
        )
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
    category: list[str] | None = Form(None),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)
    result = await db.execute(
        select(Project)
        .options(
            selectinload(Project.tags),
            selectinload(Project.categories),
        )
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
        categories=category or [],
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
    await set_project_categories(db, project, values["categories"])
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
        if description:
            # Same pipeline the detail page uses so the HTML we swap in has
            # canonical `/u/{user}/{slug}/files/{id}` URLs, not relative
            # `files/…` that would resolve differently depending on where
            # the user is viewing from.
            lookup = await get_project_file_lookup(db, project.id)
            rendered = render_for_project(
                description, user.username, project.slug, lookup
            )
        else:
            rendered = ""
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
