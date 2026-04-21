"""Admin CRUD for the curated category taxonomy."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from benchlog.categories import set_project_categories
from benchlog.models import Category, Project, ProjectStatus
from tests.conftest import csrf_token, login, make_user, post_form


async def _admin_token(client):
    return await csrf_token(client, "/admin/categories")


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


async def test_admin_list_categories_requires_admin(client, db):
    await make_user(db, email="bob@test.com", username="bob")
    await login(client, "bob")

    resp = await client.get("/admin/categories")
    assert resp.status_code == 403


async def test_admin_can_create_root_category(client, db):
    await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    await login(client, "admin")

    resp = await post_form(
        client,
        "/admin/categories",
        {
            "name": "Woodworking",
            "slug": "",  # auto-derive from name
            "parent_id": "",
            "sort_order": "10",
        },
        csrf_path="/admin/categories/new",
    )
    assert resp.status_code == 302

    cat = (
        await db.execute(select(Category).where(Category.slug == "woodworking"))
    ).scalar_one()
    assert cat.name == "Woodworking"
    assert cat.parent_id is None


async def test_admin_can_create_child_category(client, db):
    await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    parent = await _mk_cat(db, slug="electronics", name="Electronics")
    await login(client, "admin")

    resp = await post_form(
        client,
        "/admin/categories",
        {
            "name": "Arduino",
            "slug": "arduino",
            "parent_id": str(parent.id),
            "sort_order": "10",
        },
        csrf_path=f"/admin/categories/new?parent_id={parent.id}",
    )
    assert resp.status_code == 302

    child = (
        await db.execute(select(Category).where(Category.slug == "arduino"))
    ).scalar_one()
    assert child.parent_id == parent.id


async def test_admin_can_rename_category(client, db):
    await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    cat = await _mk_cat(db, slug="old", name="Old Name")
    await login(client, "admin")

    resp = await post_form(
        client,
        f"/admin/categories/{cat.id}",
        {
            "name": "New Name",
            "slug": "old",
            "parent_id": "",
            "sort_order": "5",
        },
        csrf_path=f"/admin/categories/{cat.id}/edit",
    )
    assert resp.status_code == 302
    await db.refresh(cat)
    assert cat.name == "New Name"


async def test_admin_can_reparent_category(client, db):
    await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    a = await _mk_cat(db, slug="a", name="A")
    b = await _mk_cat(db, slug="b", name="B")
    node = await _mk_cat(db, slug="movable", name="Movable", parent_id=a.id)

    await login(client, "admin")
    resp = await post_form(
        client,
        f"/admin/categories/{node.id}",
        {
            "name": "Movable",
            "slug": "movable",
            "parent_id": str(b.id),
            "sort_order": "0",
        },
        csrf_path=f"/admin/categories/{node.id}/edit",
    )
    assert resp.status_code == 302
    await db.refresh(node)
    assert node.parent_id == b.id


async def test_admin_cannot_create_cycle(client, db):
    await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    parent = await _mk_cat(db, slug="parent", name="Parent")
    child = await _mk_cat(
        db, slug="child", name="Child", parent_id=parent.id
    )
    await login(client, "admin")

    # Try to make the parent a child of its own child — would form a cycle.
    resp = await post_form(
        client,
        f"/admin/categories/{parent.id}",
        {
            "name": "Parent",
            "slug": "parent",
            "parent_id": str(child.id),
            "sort_order": "0",
        },
        csrf_path=f"/admin/categories/{parent.id}/edit",
    )
    assert resp.status_code == 400
    assert "cycle" in resp.text.lower()
    await db.refresh(parent)
    # parent_id unchanged
    assert parent.parent_id is None


async def test_admin_cannot_delete_category_with_children(client, db):
    await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    parent = await _mk_cat(db, slug="parent", name="Parent")
    await _mk_cat(db, slug="child", name="Child", parent_id=parent.id)
    await login(client, "admin")

    resp = await post_form(
        client,
        f"/admin/categories/{parent.id}/delete",
        {},
        csrf_path="/admin/categories",
    )
    # Redirects with a flash_error; category still exists.
    assert resp.status_code == 302
    still = (
        await db.execute(select(Category).where(Category.id == parent.id))
    ).scalar_one_or_none()
    assert still is not None


async def test_admin_delete_detaches_from_projects(client, db):
    admin = await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    cat = await _mk_cat(db, slug="bye", name="Bye")

    project = Project(
        user_id=admin.id,
        title="P",
        slug="p",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    await set_project_categories(db, project, [str(cat.id)])
    await db.commit()

    await login(client, "admin")
    resp = await post_form(
        client,
        f"/admin/categories/{cat.id}/delete",
        {},
        csrf_path="/admin/categories",
    )
    assert resp.status_code == 302

    # Test session persists stale identity-map state across requests
    # (expire_on_commit=False). Drop it so the next read hits the DB.
    db.expunge_all()
    remaining = (
        await db.execute(
            select(Project)
            .options(selectinload(Project.categories))
            .where(Project.id == project.id)
        )
    ).scalar_one()
    assert remaining.categories == []


# ---------- reorder (DnD) ---------- #


async def test_admin_reorder_updates_sort_order(client, db):
    # DnD fires a JSON POST with {parent_id, ordered_ids}. Server should
    # write sort_order in steps of 10 matching the submitted position.
    await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    a = await _mk_cat(db, slug="a", name="A", sort_order=10)
    b = await _mk_cat(db, slug="b", name="B", sort_order=20)
    c = await _mk_cat(db, slug="c", name="C", sort_order=30)
    await login(client, "admin")

    token = await _admin_token(client)
    # Flip to C, A, B
    resp = await client.post(
        "/admin/categories/reorder",
        json={
            "parent_id": None,
            "ordered_ids": [str(c.id), str(a.id), str(b.id)],
        },
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 204

    # Reload each and confirm the new sort_orders.
    db.expunge_all()
    rows = {
        r.slug: r
        for r in (await db.execute(select(Category))).scalars().all()
    }
    assert rows["c"].sort_order == 10
    assert rows["a"].sort_order == 20
    assert rows["b"].sort_order == 30


async def test_admin_reorder_rejects_cross_parent_ids(client, db):
    # All ordered_ids must share the submitted parent. Mixing parents is a
    # 400 — the client should only call this for sibling reorders.
    await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    root_a = await _mk_cat(db, slug="root-a")
    root_b = await _mk_cat(db, slug="root-b")
    child_under_a = await _mk_cat(
        db, slug="child-a", parent_id=root_a.id
    )
    await login(client, "admin")

    token = await _admin_token(client)
    # Claim `child_under_a` is a child of `root_b` — server should reject.
    resp = await client.post(
        "/admin/categories/reorder",
        json={
            "parent_id": str(root_b.id),
            "ordered_ids": [str(child_under_a.id)],
        },
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 400


async def test_admin_reorder_requires_admin(client, db):
    # Non-admins get the usual 403 from require_admin.
    await make_user(db, email="bob@test.com", username="bob")
    a = await _mk_cat(db, slug="a")
    await login(client, "bob")

    # bob can't hit the admin page to pull a CSRF token — use a path
    # regular users can reach. The token is session-bound, not route-bound.
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        "/admin/categories/reorder",
        json={"parent_id": None, "ordered_ids": [str(a.id)]},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 403


async def test_admin_reorder_rejects_unknown_id(client, db):
    # If the payload names an id that doesn't exist, it's a 400 — better
    # to surface a stale-DOM problem than silently no-op.
    await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    a = await _mk_cat(db, slug="a")
    await login(client, "admin")

    token = await _admin_token(client)
    bogus = uuid.uuid4()
    resp = await client.post(
        "/admin/categories/reorder",
        json={
            "parent_id": None,
            "ordered_ids": [str(a.id), str(bogus)],
        },
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 400
