import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.activity import list_project_activity, record_event
from benchlog.categories import (
    get_categories_flat,
    get_descendants_map,
    set_project_categories,
)
from benchlog.collections import (
    get_project_collection_memberships,
    list_public_collections_containing_project,
    list_user_collections,
)
from benchlog.database import get_db
from benchlog.dependencies import current_user, require_user
from benchlog.files import (
    get_project_entry_index,
    get_project_file_index,
    get_project_file_lookup,
)
from benchlog.markdown import render_for_project
from benchlog.models import (
    ActivityEventType,
    Project,
    ProjectCategory,
    ProjectFile,
    ProjectStatus,
    ProjectTag,
    RelationType,
    Tag,
    User,
)
from benchlog.project_relations import (
    filter_visible,
    get_incoming_relations,
    get_outgoing_relations,
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


async def load_project_header_ctx(
    db: AsyncSession, viewer: User | None, project: Project
) -> dict:
    """Return the full context the shared project header partial needs.

    Every route that renders ``projects/_layout.html`` (overview, journal,
    files, gallery, links, activity — plus nested detail pages that reuse
    the same header) passes this dict straight into the template so the
    header renders identically on every tab.

    Keys:
      ``viewer_collections`` — list[Collection] owned by the viewer. Empty
        list when the viewer is a guest (the partial's ``{% if user %}``
        gate hides the picker entirely in that case).
      ``project_collection_ids`` — set of viewer collection IDs that
        currently contain this project.
      ``category_breadcrumbs`` — ``{category_id_str: 'Parent › Child'}``
        for every category attached to the project. Always returned (empty
        dict when the project has no categories) so the template never
        has to branch on missing context — the header falls back to
        ``cat.name`` only when the breadcrumb lookup genuinely misses.
      ``status_chip_options`` — ``[(value, label)]`` pairs for the owner's
        status dropdown. Always returned so the shared header renders the
        same list on every tab (overview/journal/files/gallery/links).
      ``known_tags`` / ``known_categories`` — vocabularies for the inline
        Manage modals (tags + categories) that the header renders. Owner-
        only — guests get empty lists since the modals are gated on
        ``is_owner``. Loaded here (not per-route) so every tab — not just
        the overview — opens with a populated picker.
    """
    if viewer is None:
        viewer_collections: list = []
        project_collection_ids: set = set()
    else:
        viewer_collections = await list_user_collections(db, viewer.id)
        project_collection_ids = await get_project_collection_memberships(
            db, viewer.id, project.id
        )
    is_owner = viewer is not None and project.user_id == viewer.id
    known_tags: list[str] = []
    known_categories: list[dict] = []
    if is_owner:
        known_tags = await get_user_tag_slugs(db, viewer.id)
        known_categories = await get_categories_flat(db)
    category_breadcrumbs = await _breadcrumbs_for(db, project.categories)
    return {
        "viewer_collections": viewer_collections,
        "project_collection_ids": project_collection_ids,
        "category_breadcrumbs": category_breadcrumbs,
        "status_chip_options": _status_options(),
        "known_tags": known_tags,
        "known_categories": known_categories,
    }


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


def _search_tokens(q: str) -> list[str]:
    """Parse a free-text search into tokens. Punctuation becomes spaces,
    tokens shorter than 2 chars are dropped (single-letter matches scan
    too much to be useful)."""
    if not q:
        return []
    cleaned = _QUERY_TOKEN_RE.sub(" ", q)
    return [t for t in cleaned.split() if len(t) >= 2]


def _build_prefix_tsquery(q: str) -> str:
    """Turn a free-text search into a tsquery string with prefix matching
    per token. "flowi router" → "flowi:* & router:*"."""
    tokens = _search_tokens(q)
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


def _apply_search_query(query, *, q: str, title_only: bool = False):
    """Filter a Project select by substring match over title + description.

    Each token (from `_search_tokens`) must appear as a substring in
    either the title or the description. "wire" matches "Flowire";
    "flow" matches "Flowire" too.

    Why ILIKE not tsquery: tsquery doesn't support infix matching
    natively (only prefix via `:*`), and maker-journal content rarely
    benefits from stemming enough to outweigh losing substring matches.
    Ranking still uses ts_rank_cd via `_tsquery_for` for ORDER BY when
    a tsquery is buildable — ILIKE is WHERE only.

    `title_only=True` skips the description column — used by picker-style
    comboboxes (related-project search) where matching on a stray phrase
    buried in someone's description would be a surprising result.

    Returns `query` unchanged when the search has no usable tokens.
    """
    tokens = _search_tokens(q)
    if not tokens:
        return query
    for token in tokens:
        pattern = f"%{token}%"
        if title_only:
            query = query.where(Project.title.ilike(pattern))
        else:
            query = query.where(
                or_(
                    Project.title.ilike(pattern),
                    Project.description.ilike(pattern),
                )
            )
    return query


def _apply_filter_query(
    query,
    *,
    statuses: list[str],
    tags: list[str],
    tag_mode: str = "all",
    categories: list[str] | None = None,
    category_mode: str = "all",
    category_descendants_map: dict | None = None,
):
    """Apply the shared status/tag/category filters to a Project select.

    - ``statuses`` empty → exclude archived (default list behaviour).
    - ``statuses`` non-empty → keep only those statuses.
    - ``tags`` with ``tag_mode="all"`` (default) → AND: project must carry
      every selected slug. ``tag_mode="any"`` → OR: project must carry at
      least one of the selected slugs (useful for corralling spelling
      variants like "3d-printing" / "3d-printed" on Explore).
    - ``categories`` with ``category_mode="all"`` (default) → AND: project
      must be assigned to every selected category (or any of its
      descendants — see below). ``category_mode="any"`` → OR: project
      must match at least one selected category subtree. Curated
      taxonomies still benefit from the "any" escape for browsing across
      siblings.
    - ``category_descendants_map`` (optional) — ``{cat_id: {self+descendants}}``
      from `get_descendants_map`. When supplied, each requested category
      expands to its full subtree before going into the IN clause, so
      filtering by "Crafts" matches projects tagged "Crafts › Leather".
      Caller is responsible for pre-fetching it; the helper stays sync
      so the existing call shape doesn't change.
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
        # Expand each requested category to its full subtree (self +
        # descendants) so picking a parent matches projects tagged on
        # any leaf below it. Falls back to a singleton group when the
        # map is unavailable or doesn't know the id (stale URL, etc.).
        groups: list[set[str]] = []
        for raw in categories:
            try:
                cid = uuid.UUID(raw)
            except (ValueError, TypeError):
                groups.append({raw})
                continue
            subtree = (
                category_descendants_map.get(cid)
                if category_descendants_map
                else None
            )
            if subtree:
                groups.append({str(d) for d in subtree})
            else:
                groups.append({str(cid)})

        if category_mode == "all":
            # AND: project must have at least one category in *each* of
            # the requested subtrees. Per-group IN subqueries match the
            # tag pattern but skip the HAVING-count trick — counting
            # distinct ids inside an expanded subtree would mis-AND when
            # one project has multiple leaves under the same root.
            for grp in groups:
                sub = (
                    select(ProjectCategory.project_id)
                    .where(ProjectCategory.category_id.in_(grp))
                )
                query = query.where(Project.id.in_(sub))
        else:
            # any: union all subtrees, single IN.
            sub = (
                select(ProjectCategory.project_id)
                .where(ProjectCategory.category_id.in_(set().union(*groups)))
            )
            query = query.where(Project.id.in_(sub))
    return query


SHORT_DESCRIPTION_MAX_LEN = 200

# Hard cap on the markdown description, in UTF-8 bytes. The `projects` table
# carries a STORED generated `tsvector` over title + description, and Postgres
# rejects any tsvector value over ~1 MB. Embedded base64 image data URLs from
# the toast-ui editor blow past that ceiling instantly (a single phone photo
# is several MB), surfacing as an opaque 500. Reject early with a friendly
# message instead — and 256 KB is still ~250k characters of prose, well past
# anything a real description needs.
DESCRIPTION_MAX_BYTES = 256 * 1024


def _description_size_error(text: str | None) -> str | None:
    """Return a user-facing error if `text` would overflow the search index.

    Returns None when the text is fine. Callers turn the message into either
    a form re-render or a JSON 400, depending on the endpoint's contract.
    """
    if not text:
        return None
    if len(text.encode("utf-8")) <= DESCRIPTION_MAX_BYTES:
        return None
    return (
        "Description is too large. If you pasted or dropped an image into the "
        "editor it was embedded as inline data — upload it via the Files tab "
        "and link to it with `files/your-image.jpg` instead."
    )


def _clean_short_description(raw: str) -> str:
    """Trim and collapse internal whitespace runs in a short description.

    Plain text only — newlines and tabs become single spaces so the field
    always renders as a one-liner on cards. Caller still needs to enforce
    the length cap.
    """
    return re.sub(r"\s+", " ", (raw or "").strip())


def _empty_form_values() -> dict:
    return {
        "title": "",
        "slug": "",
        "description": "",
        "short_description": "",
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
        "short_description": project.short_description or "",
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
    short_description: str,
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
        "short_description": short_description,
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
    # Editors scoped to a project expose `files/…` and `journal/…`
    # typeaheads sourced from these indexes. New projects have no files
    # or entries yet, so send empty lists — the client-side code treats
    # that as "enable typeahead but no matches".
    file_index = (
        await get_project_file_index(db, project.id) if project is not None else []
    )
    entry_index = (
        await get_project_entry_index(db, project.id) if project is not None else []
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
            "entry_index": entry_index,
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
    descendants_map = (
        await get_descendants_map(db) if current_categories else None
    )
    query = _apply_filter_query(
        query,
        statuses=current_statuses,
        tags=current_tags,
        tag_mode=current_tag_mode,
        categories=current_categories,
        category_mode=current_category_mode,
        category_descendants_map=descendants_map,
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
    short_description: str = Form(""),
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
        short_description=_clean_short_description(short_description),
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
    if len(values["short_description"]) > SHORT_DESCRIPTION_MAX_LEN:
        return await fail(
            f"Short description must be {SHORT_DESCRIPTION_MAX_LEN} characters or fewer."
        )
    if (err := _description_size_error(values["description"])) is not None:
        return await fail(err)

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
        short_description=values["short_description"] or None,
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
    await db.flush()
    await record_event(
        db,
        actor=user,
        project=project,
        event_type=ActivityEventType.project_created,
    )
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
    # File + entry indexes only matter for the inline description editor
    # (owner-only), so skip the queries entirely for guests / non-owners.
    file_index = await get_project_file_index(db, project.id) if is_owner else None
    entry_index = await get_project_entry_index(db, project.id) if is_owner else None
    # Shared-header context — category breadcrumbs + viewer's collections
    # + membership set. Packed into a single dict so every project tab
    # route can unpack the same shape into the template.
    header_ctx = await load_project_header_ctx(db, user, project)
    featured_in_collections = await list_public_collections_containing_project(
        db,
        project.id,
        exclude_user_id=user.id if user is not None else None,
    )
    # Relations (outgoing + incoming), pre-filtered by the viewer's
    # visibility and grouped by type so the template iterates cleanly.
    # Owner always sees their own outgoing relations (even when target
    # is their own private project) thanks to the viewer-id match in
    # `visible_to`. Guests only see relations whose far endpoint is
    # public.
    viewer_id = user.id if user is not None else None
    outgoing_raw = await get_outgoing_relations(db, project.id)
    incoming_raw = await get_incoming_relations(db, project.id)
    outgoing_visible = filter_visible(outgoing_raw, "target", viewer_id)
    incoming_visible = filter_visible(incoming_raw, "source", viewer_id)
    outgoing_groups = _group_relations_by_type(outgoing_visible)
    incoming_groups = _group_relations_by_type(incoming_visible)
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
            **header_ctx,
            "file_index": file_index,
            "entry_index": entry_index,
            "featured_in_collections": featured_in_collections,
            "outgoing_relation_groups": outgoing_groups,
            "incoming_relation_groups": incoming_groups,
            "user_pickable_relation_types": [
                (t.value, t.label, t.icon)
                for t in (
                    RelationType.inspired_by,
                    RelationType.related_to,
                    RelationType.depends_on,
                )
            ],
            "error": request.session.pop("flash_error", None),
            "notice": request.session.pop("flash_notice", None),
        },
    )


def _group_relations_by_type(relations):
    """Return ``[(RelationType, [relation, ...]), ...]`` in enum order.

    Template-friendly shape — the detail page wants heading-per-group
    with a stable ordering matching the segmented control. Types with
    no surviving relations after visibility filtering are dropped so
    the template doesn't have to branch on emptiness per group.
    """
    by_type: dict[RelationType, list] = {}
    for r in relations:
        by_type.setdefault(r.relation_type, []).append(r)
    return [(t, by_type[t]) for t in RelationType if t in by_type]


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


@router.get("/u/{username}/{slug}/activity")
async def project_activity(
    username: str,
    slug: str,
    request: Request,
    user: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per-project activity feed — reuses the project_detail visibility gate."""
    project = await get_project_by_username_and_slug(db, username, slug)
    if project is None:
        raise HTTPException(status_code=404)
    is_owner = user is not None and project.user_id == user.id
    if not is_owner and not project.is_public:
        raise HTTPException(status_code=404)
    events = await list_project_activity(
        db, project.id, viewer_id=user.id if user is not None else None
    )
    header_ctx = await load_project_header_ctx(db, user, project)
    return templates.TemplateResponse(
        request,
        "projects/activity.html",
        {
            "user": user,
            "project": project,
            "is_owner": is_owner,
            "events": events,
            **header_ctx,
        },
    )


async def _require_owned_project(
    db: AsyncSession, user: User, username: str, slug: str
) -> Project:
    """Owner-scoped project fetch, 404 for everyone else.

    Same shape used by `update_project` and `delete_project` — centralized
    here so the new inline-edit endpoint shares a single validator. Eager
    loads the two association collections every caller ends up touching
    (tags and categories) since `Project.tags` / `Project.categories` are
    `raise_on_sql`.
    """
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
    return project


@router.post("/u/{username}/{slug}/settings")
async def update_project_settings(
    username: str,
    slug: str,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Partial-update endpoint for inline project-detail edits.

    Accepts ANY subset of:
      title, slug, status, tags (comma-separated), categories (repeated
      form field), is_public, pinned, set_public, set_pinned.

    Semantics:
      - Only fields present in the form are considered; anything missing
        stays as-is (sentinel: missing key → no change).
      - Booleans use an explicit convention: `is_public` / `pinned` can be
        sent as "1"/"true"/"on" for True or "0"/"false"/"off" for False.
        The value's presence is what triggers the update — omit the key
        entirely to leave the field alone.
      - Returns 204 No Content on success with no body.
      - When the slug changes, returns 200 with JSON
        `{"redirect": "/u/<user>/<new-slug>"}` so the client can navigate.
      - Returns 400 JSON `{"detail": "..."}` on validation failure.

    Does not emit any activity event on visibility flips — the project
    header already shows current visibility, so logging every flip is noise.
    """
    project = await _require_owned_project(db, user, username, slug)
    form = await request.form()
    TRUE_VALUES = {"1", "true", "on", "yes"}
    FALSE_VALUES = {"0", "false", "off", "no", ""}

    def _coerce_bool(raw) -> bool | None:
        if raw is None:
            return None
        text = str(raw).strip().lower()
        if text in TRUE_VALUES:
            return True
        if text in FALSE_VALUES:
            return False
        return None

    slug_changed = False

    # Title — present and non-empty to update; present and empty is a 400
    # (mirrors the full form).
    if "title" in form:
        new_title = str(form.get("title") or "").strip()
        if not new_title:
            return JSONResponse({"detail": "Title is required."}, status_code=400)
        if len(new_title) > 256:
            return JSONResponse(
                {"detail": "Title must be 256 characters or fewer."}, status_code=400
            )
        project.title = new_title

    # Short description — present and empty clears it; present and non-empty
    # updates. Whitespace is folded to single spaces and trimmed so the field
    # always renders as a single line on cards.
    if "short_description" in form:
        cleaned = _clean_short_description(str(form.get("short_description") or ""))
        if len(cleaned) > SHORT_DESCRIPTION_MAX_LEN:
            return JSONResponse(
                {
                    "detail": (
                        f"Short description must be {SHORT_DESCRIPTION_MAX_LEN} "
                        "characters or fewer."
                    )
                },
                status_code=400,
            )
        project.short_description = cleaned or None

    # Slug — normalize, dedupe against this user's other projects.
    if "slug" in form:
        raw_slug = str(form.get("slug") or "").strip()
        normalized = normalize_slug(raw_slug)
        if not normalized:
            return JSONResponse(
                {"detail": "Slug must contain letters or numbers."}, status_code=400
            )
        if normalized != project.slug:
            if await is_slug_taken(
                db, user.id, normalized, exclude_project_id=project.id
            ):
                return JSONResponse(
                    {
                        "detail": f"“{normalized}” is already used by another of your projects."
                    },
                    status_code=400,
                )
            project.slug = normalized
            slug_changed = True

    # Status — must parse to a known enum value.
    if "status" in form:
        raw_status = str(form.get("status") or "")
        parsed = _parse_status(raw_status)
        if parsed is None:
            return JSONResponse(
                {"detail": "Unknown status."}, status_code=400
            )
        project.status = parsed

    # Booleans: is_public / pinned. Accept either the field itself (with
    # true/false-ish values) or a `set_X` flag — either way, presence +
    # truthy/falsy coercion.
    if "is_public" in form:
        coerced = _coerce_bool(form.get("is_public"))
        if coerced is None:
            return JSONResponse(
                {"detail": "Invalid value for is_public."}, status_code=400
            )
        project.is_public = coerced
    if "pinned" in form:
        coerced = _coerce_bool(form.get("pinned"))
        if coerced is None:
            return JSONResponse(
                {"detail": "Invalid value for pinned."}, status_code=400
            )
        project.pinned = coerced

    # Tags: comma-separated string, parsed via parse_tag_input.
    if "tags" in form:
        raw_tags = str(form.get("tags") or "")
        await set_project_tags(db, project, parse_tag_input(raw_tags))

    # Categories: repeated form values. Accept both `categories` and
    # `category` names — the combobox partial emits `category` by default.
    categories_present = "categories" in form or "category" in form
    if categories_present:
        raw_cats = form.getlist("categories") or form.getlist("category")
        await set_project_categories(db, project, _clean_category_list(raw_cats))

    await db.commit()

    if slug_changed:
        return JSONResponse(
            {"redirect": f"/u/{user.username}/{project.slug}"}, status_code=200
        )
    # 204 No Content — client refreshes its local view without touching URL.
    return Response(status_code=204)


@router.post("/u/{username}/{slug}")
async def update_project(
    username: str,
    slug: str,
    request: Request,
    title: str = Form(""),
    new_slug: str = Form("", alias="slug"),
    description: str = Form(""),
    short_description: str = Form(""),
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
        short_description=_clean_short_description(short_description),
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
    if len(values["short_description"]) > SHORT_DESCRIPTION_MAX_LEN:
        return await fail(
            f"Short description must be {SHORT_DESCRIPTION_MAX_LEN} characters or fewer."
        )
    if (err := _description_size_error(values["description"])) is not None:
        return await fail(err)

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
    project.short_description = values["short_description"] or None
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

    if (err := _description_size_error(description)) is not None:
        # JSON: surfaced inline in the editor's error span. Form fallback:
        # surface via the flash channel that the project detail already
        # renders, then redirect back so the no-JS path stays consistent
        # with the rest of the project edit flow.
        if is_json:
            raise HTTPException(status_code=400, detail=err)
        request.session["flash_error"] = err
        return RedirectResponse(
            f"/u/{user.username}/{project.slug}", status_code=302
        )

    # Matches the main edit path: empty → NULL.
    project.description = description or None
    await db.commit()

    if is_json:
        if description:
            # Same pipeline the detail page uses so the HTML we swap in has
            # canonical `/u/{user}/{slug}/files/{id}` URLs, not relative
            # `files/…` that would resolve differently depending on where
            # the user is viewing from.
            lookup = await get_project_file_lookup(db, project.id)
            # Owner-only endpoint — pass is_owner=True so excalidraw embeds
            # in the swapped HTML get the editable affordance immediately.
            rendered = render_for_project(
                description, user.username, project.slug, lookup, is_owner=True
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
