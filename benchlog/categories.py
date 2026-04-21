"""Helpers for the curated category taxonomy.

Categories are nested; we eager-load the whole tree in one shot and build
breadcrumb strings in Python rather than walking parents with `raise_on_sql`
relationships hot. Breadcrumbs are used both for the searchable picker on
the project form (`Parent › Child` combobox labels) and for display on the
project detail header.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.models import Category

# Breadcrumb separator — `›` is a single character, renders clean in small
# chips, and reads as hierarchy in screenreaders when paired with the
# aria-label on the link.
BREADCRUMB_SEP = " › "


def _node_dict(cat: Category, breadcrumb: str) -> dict:
    return {
        "id": cat.id,
        "parent_id": cat.parent_id,
        "slug": cat.slug,
        "name": cat.name,
        "sort_order": cat.sort_order,
        "breadcrumb": breadcrumb,
    }


async def _all_categories(db: AsyncSession) -> list[Category]:
    """Fetch every category ordered by (parent_id, sort_order, name).

    Ordering here drives the in-memory tree walk below — siblings come out
    of the DB already sorted for rendering.
    """
    result = await db.execute(
        select(Category).order_by(
            Category.sort_order, Category.name
        )
    )
    return list(result.scalars().all())


def _build_breadcrumb_maps(
    cats: list[Category],
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, list[str]]]:
    """Build two id→breadcrumb maps: joined string form and segment-list form.

    The joined form (`'Parent › Child'`) is what tests assert against and
    what tooltips use. The segment-list form is what the rendering
    templates iterate over to place Lucide chevron icons between parts.

    Works for arbitrary nesting depth; resolves each node's full path by
    walking up via a `by_id` dict instead of the lazy `parent` relationship
    (which is `raise_on_sql`).
    """
    by_id: dict[uuid.UUID, Category] = {c.id: c for c in cats}
    string_map: dict[uuid.UUID, str] = {}
    parts_map: dict[uuid.UUID, list[str]] = {}
    for cat in cats:
        parts: list[str] = []
        cursor: Category | None = cat
        # Guard against malformed data (shouldn't happen — the admin
        # cycle check prevents it — but a bounded loop is cheap insurance).
        seen: set[uuid.UUID] = set()
        while cursor is not None and cursor.id not in seen:
            seen.add(cursor.id)
            parts.append(cursor.name)
            cursor = by_id.get(cursor.parent_id) if cursor.parent_id else None
        ordered = list(reversed(parts))
        parts_map[cat.id] = ordered
        string_map[cat.id] = BREADCRUMB_SEP.join(ordered)
    return string_map, parts_map


# Back-compat shim for any caller that only wants the joined string map.
def _build_breadcrumb_map(cats: list[Category]) -> dict[uuid.UUID, str]:
    return _build_breadcrumb_maps(cats)[0]


async def get_category_tree(db: AsyncSession) -> list[dict]:
    """Return the full taxonomy as a nested list of dicts.

    Each node: ``{id, slug, name, sort_order, breadcrumb, children}``. The
    ``breadcrumb`` is the full parent › child string. Children are sorted
    by (sort_order, name) thanks to the ordering in ``_all_categories``.
    """
    cats = await _all_categories(db)
    breadcrumbs = _build_breadcrumb_map(cats)

    # Group by parent
    by_parent: dict[uuid.UUID | None, list[Category]] = {}
    for cat in cats:
        by_parent.setdefault(cat.parent_id, []).append(cat)

    def build(parent_id: uuid.UUID | None) -> list[dict]:
        return [
            {
                **_node_dict(cat, breadcrumbs[cat.id]),
                "children": build(cat.id),
            }
            for cat in by_parent.get(parent_id, [])
        ]

    return build(None)


async def get_categories_flat(db: AsyncSession) -> list[dict]:
    """Flattened list for a searchable picker.

    Each entry: ``{id, slug, name, breadcrumb, breadcrumb_parts}``. Sorted
    by breadcrumb alphabetically so the combobox search feel is consistent
    regardless of ``sort_order`` tweaks at individual levels.
    `breadcrumb_parts` is the same path as a list — templates use it to
    render Lucide chevron icons between segments instead of the literal
    `›` character.
    """
    cats = await _all_categories(db)
    string_map, parts_map = _build_breadcrumb_maps(cats)
    flat = [
        {
            "id": cat.id,
            "slug": cat.slug,
            "name": cat.name,
            "breadcrumb": string_map[cat.id],
            "breadcrumb_parts": parts_map[cat.id],
        }
        for cat in cats
    ]
    flat.sort(key=lambda x: x["breadcrumb"].lower())
    return flat


async def get_category_by_slug_path(
    db: AsyncSession, slugs: list[str]
) -> Category | None:
    """Resolve a list like ``['3d-printing', 'fdm']`` to the leaf node.

    Used by filter routes that accept human-readable paths rather than
    UUIDs. Returns None on any mismatch along the way.
    """
    if not slugs:
        return None
    parent_id: uuid.UUID | None = None
    current: Category | None = None
    for slug in slugs:
        stmt = select(Category).where(Category.slug == slug)
        if parent_id is None:
            stmt = stmt.where(Category.parent_id.is_(None))
        else:
            stmt = stmt.where(Category.parent_id == parent_id)
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        current = row
        parent_id = row.id
    return current


def _coerce_uuids(raw_ids: list[str]) -> list[uuid.UUID]:
    """Parse a list of string ids; silently drop anything non-UUID."""
    out: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for value in raw_ids:
        try:
            parsed = uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            continue
        if parsed in seen:
            continue
        seen.add(parsed)
        out.append(parsed)
    return out


async def set_project_categories(
    db: AsyncSession, project, category_ids: list[str]
) -> None:
    """Replace the project's category set.

    - UUIDs; invalid ones dropped silently.
    - Non-existent IDs dropped silently (so a stale form submission can't
      500 the request).
    - Caller owns the commit.

    Implementation: assigns through the ORM's ``project.categories``
    relationship so the session's unit-of-work syncs the ``project_categories``
    secondary table cleanly. (An earlier iteration used raw DELETE+INSERT
    and got bitten by the ORM re-inserting the in-memory collection on the
    next flush — don't go back there.) The relationship is ``raise_on_sql``
    so we preload it via ``db.refresh`` when the collection isn't already
    hot — lets callers hand us freshly-committed instances without having
    to remember the eager-load gymnastics.
    """
    from sqlalchemy import inspect as sa_inspect

    state = sa_inspect(project)
    if "categories" in state.unloaded:
        await db.refresh(project, ["categories"])

    parsed = _coerce_uuids(category_ids)
    if not parsed:
        project.categories = []
        return

    result = await db.execute(
        select(Category).where(Category.id.in_(parsed))
    )
    rows = list(result.scalars().all())
    # Preserve the caller's order — easier to reason about in tests than
    # whatever order the IN came back in.
    by_id = {cat.id: cat for cat in rows}
    ordered = [by_id[cid] for cid in parsed if cid in by_id]
    project.categories = ordered
