"""Tests for the curated category taxonomy — models + helpers."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from benchlog.categories import (
    get_categories_flat,
    get_category_by_slug_path,
    get_category_tree,
    get_descendants_map,
    set_project_categories,
)
from benchlog.models import Category, Project, ProjectStatus
from tests.conftest import make_user


async def _mk_cat(
    db,
    *,
    slug: str,
    name: str | None = None,
    parent_id: uuid.UUID | None = None,
    sort_order: int = 0,
) -> Category:
    cat = Category(
        slug=slug,
        name=name or slug.title(),
        parent_id=parent_id,
        sort_order=sort_order,
    )
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    return cat


# ---------- model basics ----------


async def test_category_model_basics(db):
    parent = await _mk_cat(db, slug="woodworking", name="Woodworking")
    child = await _mk_cat(
        db, slug="joinery", name="Joinery", parent_id=parent.id
    )

    # Re-read with relationships eager-loaded
    reloaded = (
        await db.execute(
            select(Category)
            .options(selectinload(Category.children))
            .where(Category.id == parent.id)
        )
    ).scalar_one()
    assert [c.id for c in reloaded.children] == [child.id]

    reloaded_child = (
        await db.execute(
            select(Category)
            .options(selectinload(Category.parent))
            .where(Category.id == child.id)
        )
    ).scalar_one()
    assert reloaded_child.parent is not None
    assert reloaded_child.parent.id == parent.id


async def test_category_slug_uniqueness_scoped_to_parent(db):
    a = await _mk_cat(db, slug="alpha", name="Alpha")
    b = await _mk_cat(db, slug="beta", name="Beta")

    # "other" under two different parents: fine
    await _mk_cat(db, slug="other", name="Other", parent_id=a.id)
    await _mk_cat(db, slug="other", name="Other", parent_id=b.id)

    # Two "other" under the SAME parent: IntegrityError
    dup = Category(slug="other", name="Other Two", parent_id=a.id)
    db.add(dup)
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


# ---------- helpers ----------


async def test_get_category_tree(db):
    parent = await _mk_cat(db, slug="3d-printing", name="3D Printing", sort_order=10)
    await _mk_cat(db, slug="fdm", name="FDM", parent_id=parent.id, sort_order=10)
    await _mk_cat(db, slug="resin", name="Resin", parent_id=parent.id, sort_order=20)

    tree = await get_category_tree(db)
    assert len(tree) == 1
    root = tree[0]
    assert root["name"] == "3D Printing"
    assert root["breadcrumb"] == "3D Printing"
    assert [c["name"] for c in root["children"]] == ["FDM", "Resin"]
    assert root["children"][0]["breadcrumb"] == "3D Printing \u203a FDM"


async def test_get_categories_flat_sorted_by_breadcrumb(db):
    wood = await _mk_cat(db, slug="woodworking", name="Woodworking")
    prn = await _mk_cat(db, slug="3d-printing", name="3D Printing")
    await _mk_cat(db, slug="fdm", name="FDM", parent_id=prn.id)
    await _mk_cat(db, slug="joinery", name="Joinery", parent_id=wood.id)

    flat = await get_categories_flat(db)
    breadcrumbs = [row["breadcrumb"] for row in flat]
    # Alphabetical by breadcrumb — "3D Printing" before "3D Printing › FDM"
    # (because the parent is a prefix), then Woodworking nodes.
    assert breadcrumbs == sorted(breadcrumbs, key=str.lower)
    assert "3D Printing" in breadcrumbs
    assert "3D Printing \u203a FDM" in breadcrumbs
    assert "Woodworking" in breadcrumbs
    assert "Woodworking \u203a Joinery" in breadcrumbs


async def test_get_category_by_slug_path(db):
    parent = await _mk_cat(db, slug="electronics", name="Electronics")
    child = await _mk_cat(
        db, slug="arduino", name="Arduino", parent_id=parent.id
    )

    found = await get_category_by_slug_path(db, ["electronics", "arduino"])
    assert found is not None
    assert found.id == child.id

    assert await get_category_by_slug_path(db, ["electronics"]) is not None
    assert await get_category_by_slug_path(db, ["nope"]) is None
    assert await get_category_by_slug_path(db, []) is None


async def test_set_project_categories_replace_semantics(db):
    user = await make_user(db, email="alice@test.com", username="alice")
    a = await _mk_cat(db, slug="a", name="A")
    b = await _mk_cat(db, slug="b", name="B")
    c = await _mk_cat(db, slug="c", name="C")

    project = Project(
        user_id=user.id,
        title="Replace me",
        slug="replace-me",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    await set_project_categories(db, project, [str(a.id), str(b.id)])
    await db.commit()

    reloaded = (
        await db.execute(
            select(Project)
            .options(selectinload(Project.categories))
            .where(Project.id == project.id)
        )
    ).scalar_one()
    assert sorted(cat.slug for cat in reloaded.categories) == ["a", "b"]

    # Replace wholesale.
    await set_project_categories(db, reloaded, [str(b.id), str(c.id)])
    await db.commit()

    reloaded2 = (
        await db.execute(
            select(Project)
            .options(selectinload(Project.categories))
            .where(Project.id == project.id)
        )
    ).scalar_one()
    assert sorted(cat.slug for cat in reloaded2.categories) == ["b", "c"]


async def test_set_project_categories_drops_unknown_ids(db):
    user = await make_user(db, email="alice@test.com", username="alice")
    good = await _mk_cat(db, slug="good", name="Good")

    project = Project(
        user_id=user.id,
        title="x",
        slug="x",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    bogus = str(uuid.uuid4())
    await set_project_categories(
        db, project, [str(good.id), bogus, "not-a-uuid"]
    )
    await db.commit()

    reloaded = (
        await db.execute(
            select(Project)
            .options(selectinload(Project.categories))
            .where(Project.id == project.id)
        )
    ).scalar_one()
    assert [c.slug for c in reloaded.categories] == ["good"]


async def test_get_descendants_map(db):
    crafts = await _mk_cat(db, slug="crafts", name="Crafts")
    leather = await _mk_cat(
        db, slug="leather", name="Leather", parent_id=crafts.id
    )
    pottery = await _mk_cat(
        db, slug="pottery", name="Pottery", parent_id=crafts.id
    )
    elec = await _mk_cat(db, slug="elec", name="Electronics")

    descendants = await get_descendants_map(db)

    # Each node always includes itself.
    assert descendants[leather.id] == {leather.id}
    assert descendants[elec.id] == {elec.id}
    # Parent contains the whole subtree (self + immediate children).
    assert descendants[crafts.id] == {crafts.id, leather.id, pottery.id}


async def test_categories_flat_includes_ancestor_ids(db):
    crafts = await _mk_cat(db, slug="crafts", name="Crafts")
    leather = await _mk_cat(
        db, slug="leather", name="Leather", parent_id=crafts.id
    )
    nested = await _mk_cat(
        db, slug="vegtan", name="Veg Tan", parent_id=leather.id
    )

    flat = await get_categories_flat(db)
    by_id = {row["id"]: row for row in flat}

    # Top-level node has no ancestors.
    assert by_id[crafts.id]["ancestor_ids"] == []
    # One level deep — single parent.
    assert by_id[leather.id]["ancestor_ids"] == [str(crafts.id)]
    # Two levels deep — closest parent first, root last.
    assert by_id[nested.id]["ancestor_ids"] == [
        str(leather.id),
        str(crafts.id),
    ]


async def test_set_project_categories_drops_ancestor_with_descendant(db):
    user = await make_user(db, email="alice@test.com", username="alice")
    crafts = await _mk_cat(db, slug="crafts", name="Crafts")
    leather = await _mk_cat(
        db, slug="leather", name="Leather", parent_id=crafts.id
    )

    project = Project(
        user_id=user.id,
        title="Wallet",
        slug="wallet",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    # Both submitted — only the descendant should remain.
    await set_project_categories(
        db, project, [str(crafts.id), str(leather.id)]
    )
    await db.commit()

    reloaded = (
        await db.execute(
            select(Project)
            .options(selectinload(Project.categories))
            .where(Project.id == project.id)
        )
    ).scalar_one()
    assert [c.slug for c in reloaded.categories] == ["leather"]


async def test_delete_category_with_children_blocked(db):
    parent = await _mk_cat(db, slug="parent", name="Parent")
    await _mk_cat(db, slug="child", name="Child", parent_id=parent.id)

    await db.execute(
        select(Category).where(Category.id == parent.id)
    )
    await db.delete(parent)
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_delete_category_detaches_from_projects(db):
    user = await make_user(db, email="alice@test.com", username="alice")
    cat = await _mk_cat(db, slug="gone-soon", name="Gone Soon")

    project = Project(
        user_id=user.id,
        title="Keeper",
        slug="keeper",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    await set_project_categories(db, project, [str(cat.id)])
    await db.commit()

    # Delete the category — the association cascades away, project stays.
    await db.delete(cat)
    await db.commit()
    # Test session uses expire_on_commit=False, so the identity map still
    # holds a stale `project.categories`. Expunge to drop the cached view
    # and force a real round-trip on the next query.
    db.expunge_all()

    remaining = (
        await db.execute(
            select(Project)
            .options(selectinload(Project.categories))
            .where(Project.id == project.id)
        )
    ).scalar_one()
    assert remaining.categories == []
