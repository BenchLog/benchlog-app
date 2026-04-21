"""Admin CRUD for the curated category taxonomy.

Category nodes are editable live — renames, reparents, and deletions are
allowed, with two guards:
- a parent can't become a descendant of itself (cycle check),
- a category with children can't be deleted (RESTRICT at the DB level,
  surfaced as a friendly 400 in the UI).
"""

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.categories import (
    get_categories_flat,
    get_category_tree,
)
from benchlog.database import get_db
from benchlog.dependencies import require_admin
from benchlog.models import Category, ProjectCategory, User
from benchlog.projects import normalize_slug
from benchlog.templating import templates

router = APIRouter(prefix="/categories")


async def _get_category(db: AsyncSession, category_id: uuid.UUID) -> Category:
    result = await db.execute(
        select(Category).where(Category.id == category_id)
    )
    cat = result.scalar_one_or_none()
    if cat is None:
        raise HTTPException(404)
    return cat


async def _parent_choices(
    db: AsyncSession, exclude_descendants_of: uuid.UUID | None = None
) -> list[dict]:
    """Return all categories (breadcrumb-labelled) usable as a parent.

    When editing a node, we exclude the node itself and all of its
    descendants so the admin can't pick a parent that would form a cycle.
    """
    flat = await get_categories_flat(db)
    if exclude_descendants_of is None:
        return flat
    exclude = await _descendant_ids(db, exclude_descendants_of)
    exclude.add(exclude_descendants_of)
    return [row for row in flat if row["id"] not in exclude]


async def _descendant_ids(
    db: AsyncSession, root_id: uuid.UUID
) -> set[uuid.UUID]:
    """All ids in the subtree rooted at ``root_id`` (exclusive of root)."""
    all_cats = (await db.execute(select(Category))).scalars().all()
    by_parent: dict[uuid.UUID | None, list[Category]] = {}
    for c in all_cats:
        by_parent.setdefault(c.parent_id, []).append(c)
    out: set[uuid.UUID] = set()
    stack: list[uuid.UUID] = [root_id]
    while stack:
        cur = stack.pop()
        for child in by_parent.get(cur, []):
            if child.id in out:
                continue
            out.add(child.id)
            stack.append(child.id)
    return out


async def _would_form_cycle(
    db: AsyncSession, node_id: uuid.UUID, new_parent_id: uuid.UUID | None
) -> bool:
    """Would setting ``new_parent_id`` as ``node_id``'s parent form a cycle?

    Implementation: walk up from ``new_parent_id`` via the parent chain. If
    we hit ``node_id`` along the way, setting it as parent would loop.
    """
    if new_parent_id is None:
        return False
    if new_parent_id == node_id:
        return True
    # Walk ancestors of the proposed new parent — if node_id is among them,
    # node_id is an ancestor of new_parent_id, so making new_parent_id the
    # new parent would close the loop.
    cursor: uuid.UUID | None = new_parent_id
    visited: set[uuid.UUID] = set()
    while cursor is not None and cursor not in visited:
        visited.add(cursor)
        if cursor == node_id:
            return True
        parent_id = (
            await db.execute(
                select(Category.parent_id).where(Category.id == cursor)
            )
        ).scalar_one_or_none()
        cursor = parent_id
    return False


# ---------- views ----------


@router.get("")
async def list_categories(
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tree = await get_category_tree(db)
    # Per-category project counts so admins see deletion blast radius.
    counts_result = await db.execute(
        select(ProjectCategory.category_id, func.count().label("n")).group_by(
            ProjectCategory.category_id
        )
    )
    counts = {row.category_id: row.n for row in counts_result}
    # Parent-choice options for the create/edit modal. We don't filter
    # descendants here — the server rejects cycles on submit (see
    # `_would_form_cycle`), which keeps the client code simple.
    parent_choices = await _parent_choices(db)
    error = request.session.pop("flash_error", None)
    notice = request.session.pop("flash_notice", None)
    return templates.TemplateResponse(
        request,
        "admin/categories_list.html",
        {
            "user": admin,
            "tree": tree,
            "counts": counts,
            "parent_choices": parent_choices,
            "error": error,
            "notice": notice,
        },
    )


@router.get("/new")
async def new_category_form(
    request: Request,
    parent_id: str | None = Query(None),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    parent_uuid: uuid.UUID | None = None
    if parent_id:
        try:
            parent_uuid = uuid.UUID(parent_id)
        except ValueError:
            parent_uuid = None
    parents = await _parent_choices(db)
    form_values = {
        "name": "",
        "slug": "",
        "parent_id": str(parent_uuid) if parent_uuid else "",
    }
    return templates.TemplateResponse(
        request,
        "admin/categories_form.html",
        {
            "user": admin,
            "category": None,
            "form_values": form_values,
            "parent_choices": parents,
            "error": None,
        },
    )


@router.post("")
async def create_category(
    request: Request,
    name: str = Form(""),
    slug: str = Form(""),
    parent_id: str = Form(""),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    # `sort_order` is DnD-managed via /admin/categories/reorder and
    # auto-assigned below, so it's not part of the create-form contract.
    values = {
        "name": name.strip(),
        "slug": slug.strip(),
        "parent_id": parent_id,
    }
    parents = await _parent_choices(db)
    json_mode = "application/json" in request.headers.get("accept", "")

    async def fail(msg: str, status: int = 400):
        if json_mode:
            return JSONResponse({"detail": msg}, status_code=status)
        return templates.TemplateResponse(
            request,
            "admin/categories_form.html",
            {
                "user": admin,
                "category": None,
                "form_values": values,
                "parent_choices": parents,
                "error": msg,
            },
            status_code=status,
        )

    if not values["name"]:
        return await fail("Name is required.")
    # Slug defaults to the slugified name so admins don't have to type it.
    candidate_slug = normalize_slug(values["slug"] or values["name"])
    if not candidate_slug:
        return await fail("Slug must contain letters or numbers.")

    parent_uuid: uuid.UUID | None = None
    if values["parent_id"]:
        try:
            parent_uuid = uuid.UUID(values["parent_id"])
        except ValueError:
            return await fail("Invalid parent.")
        if (
            await db.execute(
                select(Category.id).where(Category.id == parent_uuid)
            )
        ).scalar_one_or_none() is None:
            return await fail("Parent not found.")

    # Auto-assign sort_order: step-of-10 past the current max among
    # siblings, so new nodes land at the end of their parent and the gaps
    # leave room for manual-ish reorders (via DnD on the list page) to
    # compute intermediate values if ever needed.
    max_order_stmt = select(
        func.coalesce(func.max(Category.sort_order), 0)
    )
    if parent_uuid is None:
        max_order_stmt = max_order_stmt.where(Category.parent_id.is_(None))
    else:
        max_order_stmt = max_order_stmt.where(Category.parent_id == parent_uuid)
    current_max = (await db.execute(max_order_stmt)).scalar_one() or 0
    sort_value = int(current_max) + 10

    cat = Category(
        parent_id=parent_uuid,
        slug=candidate_slug,
        name=values["name"],
        sort_order=sort_value,
    )
    db.add(cat)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return await fail(
            f"A category with slug \u201c{candidate_slug}\u201d already exists under this parent."
        )
    if json_mode:
        return JSONResponse({"id": str(cat.id), "name": cat.name}, status_code=201)
    request.session["flash_notice"] = f"Created \u201c{cat.name}\u201d."
    return RedirectResponse("/admin/categories", status_code=302)


@router.post("/reorder")
async def reorder_categories(
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Persist a new sibling order after a drag-and-drop on the list page.

    Body (JSON): ``{"parent_id": "<uuid>" | null, "ordered_ids": ["<uuid>", …]}``.
    All `ordered_ids` must already be children of `parent_id` — this
    endpoint **does not re-parent**; use the edit form for that (cycle
    check etc. lives there). Returns 204 on success.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    parent_raw = payload.get("parent_id")
    parent_uuid: uuid.UUID | None = None
    if parent_raw not in (None, ""):
        try:
            parent_uuid = uuid.UUID(str(parent_raw))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid parent_id.")

    ids_raw = payload.get("ordered_ids") or []
    if not isinstance(ids_raw, list):
        raise HTTPException(
            status_code=400, detail="ordered_ids must be a list."
        )

    target_ids: list[uuid.UUID] = []
    for raw in ids_raw:
        try:
            target_ids.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail="Invalid category id."
            )

    if not target_ids:
        return Response(status_code=204)

    # Fetch + validate: every id must exist AND share the submitted parent.
    # Protects against a stale DOM state trying to reorder across parents.
    rows = (
        await db.execute(
            select(Category).where(Category.id.in_(target_ids))
        )
    ).scalars().all()
    by_id = {c.id: c for c in rows}
    if len(by_id) != len(set(target_ids)):
        raise HTTPException(
            status_code=400, detail="Unknown category id in payload."
        )
    for cat in rows:
        if cat.parent_id != parent_uuid:
            raise HTTPException(
                status_code=400,
                detail="Category doesn't belong to the submitted parent.",
            )

    # Write sort_order in steps of 10 so future manual inserts have room.
    for position, cid in enumerate(target_ids):
        by_id[cid].sort_order = (position + 1) * 10
    await db.commit()
    return Response(status_code=204)


@router.get("/{category_id}/edit")
async def edit_category_form(
    category_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    cat = await _get_category(db, category_id)
    parents = await _parent_choices(db, exclude_descendants_of=cat.id)
    form_values = {
        "name": cat.name,
        "slug": cat.slug,
        "parent_id": str(cat.parent_id) if cat.parent_id else "",
    }
    return templates.TemplateResponse(
        request,
        "admin/categories_form.html",
        {
            "user": admin,
            "category": cat,
            "form_values": form_values,
            "parent_choices": parents,
            "error": None,
        },
    )


@router.post("/{category_id}")
async def update_category(
    category_id: uuid.UUID,
    request: Request,
    name: str = Form(""),
    slug: str = Form(""),
    parent_id: str = Form(""),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    # `sort_order` is DnD-managed via /admin/categories/reorder; unchanged
    # on a same-parent edit, re-anchored at the end of the new parent's
    # siblings on a parent change (see the block near the commit call).
    cat = await _get_category(db, category_id)
    values = {
        "name": name.strip(),
        "slug": slug.strip(),
        "parent_id": parent_id,
    }
    parents = await _parent_choices(db, exclude_descendants_of=cat.id)
    json_mode = "application/json" in request.headers.get("accept", "")

    async def fail(msg: str, status: int = 400):
        if json_mode:
            return JSONResponse({"detail": msg}, status_code=status)
        return templates.TemplateResponse(
            request,
            "admin/categories_form.html",
            {
                "user": admin,
                "category": cat,
                "form_values": values,
                "parent_choices": parents,
                "error": msg,
            },
            status_code=status,
        )

    if not values["name"]:
        return await fail("Name is required.")
    candidate_slug = normalize_slug(values["slug"] or values["name"])
    if not candidate_slug:
        return await fail("Slug must contain letters or numbers.")

    parent_uuid: uuid.UUID | None = None
    if values["parent_id"]:
        try:
            parent_uuid = uuid.UUID(values["parent_id"])
        except ValueError:
            return await fail("Invalid parent.")
        if parent_uuid == cat.id:
            return await fail("A category can't be its own parent.")
        if await _would_form_cycle(db, cat.id, parent_uuid):
            return await fail(
                "Can't set that as the parent — it would create a cycle."
            )
        if (
            await db.execute(
                select(Category.id).where(Category.id == parent_uuid)
            )
        ).scalar_one_or_none() is None:
            return await fail("Parent not found.")

    # Sort order is DnD-managed via /admin/categories/reorder, so the edit
    # form doesn't expose it. On a parent change we re-anchor the node at
    # the end of its new siblings; otherwise leave the existing value
    # alone so unrelated edits (rename, slug fix) don't reshuffle.
    if cat.parent_id != parent_uuid:
        max_order_stmt = select(
            func.coalesce(func.max(Category.sort_order), 0)
        )
        if parent_uuid is None:
            max_order_stmt = max_order_stmt.where(Category.parent_id.is_(None))
        else:
            max_order_stmt = max_order_stmt.where(
                Category.parent_id == parent_uuid
            )
        current_max = (await db.execute(max_order_stmt)).scalar_one() or 0
        cat.sort_order = int(current_max) + 10

    cat.name = values["name"]
    cat.slug = candidate_slug
    cat.parent_id = parent_uuid
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return await fail(
            f"A category with slug \u201c{candidate_slug}\u201d already exists under this parent."
        )
    if json_mode:
        return JSONResponse({"id": str(cat.id), "name": cat.name}, status_code=200)
    request.session["flash_notice"] = f"Saved \u201c{cat.name}\u201d."
    return RedirectResponse("/admin/categories", status_code=302)


@router.post("/{category_id}/delete")
async def delete_category(
    category_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    cat = await _get_category(db, category_id)
    # Eager-load children so we can check whether to block.
    children_result = await db.execute(
        select(func.count()).where(Category.parent_id == cat.id)
    )
    child_count = children_result.scalar_one()
    if child_count:
        request.session["flash_error"] = (
            f"Can't delete \u201c{cat.name}\u201d — it has {child_count} "
            "sub-categor" + ("ies" if child_count != 1 else "y") + ". Delete or reparent them first."
        )
        return RedirectResponse("/admin/categories", status_code=302)
    name = cat.name
    await db.delete(cat)
    await db.commit()
    request.session["flash_notice"] = f"Deleted \u201c{name}\u201d."
    return RedirectResponse("/admin/categories", status_code=302)


# Re-export relationship for admin registry
__all__ = ["router"]
