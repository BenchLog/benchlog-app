"""Tests for Collections — user-curated named groups of projects.

Mirrors test_projects.py patterns: access control, slug collisions,
visibility, and the AJAX toggle endpoint used by the add-to-collections
modal on the project detail page.
"""

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from benchlog.models import Collection, Project, ProjectStatus
from tests.conftest import csrf_token, login, make_user, post_form


async def _make_project(db, user, *, title, slug, is_public=False, status=ProjectStatus.idea):
    p = Project(
        user_id=user.id,
        title=title,
        slug=slug,
        status=status,
        is_public=is_public,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def test_create_collection(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    resp = await post_form(
        client,
        "/u/alice/collections",
        {
            "name": "Guitar builds",
            "slug": "guitar-builds",
            "description": "Everything with strings.",
            "is_public": "1",
        },
        csrf_path="/u/alice/collections/new",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/collections/guitar-builds"

    row = (
        await db.execute(select(Collection).where(Collection.slug == "guitar-builds"))
    ).scalar_one()
    assert row.name == "Guitar builds"
    assert row.is_public is True
    assert row.description == "Everything with strings."


async def test_create_collection_auto_slug_from_name(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    resp = await post_form(
        client,
        "/u/alice/collections",
        {"name": "ML Projects", "slug": "", "description": ""},
        csrf_path="/u/alice/collections/new",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/collections/ml-projects"


async def test_create_collection_slug_collision_appends_counter(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    for _ in range(3):
        await post_form(
            client,
            "/u/alice/collections",
            {"name": "Studio", "slug": "", "description": ""},
            csrf_path="/u/alice/collections/new",
        )

    slugs = sorted(
        s for s in (await db.execute(select(Collection.slug))).scalars().all()
    )
    assert slugs == ["studio", "studio-2", "studio-3"]


async def test_two_users_can_share_the_same_collection_slug(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")

    await login(client, "alice")
    await post_form(
        client,
        "/u/alice/collections",
        {"name": "Guitars", "slug": "guitars", "description": ""},
        csrf_path="/u/alice/collections/new",
    )

    await login(client, "bob")
    resp = await post_form(
        client,
        "/u/bob/collections",
        {"name": "Guitars", "slug": "guitars", "description": ""},
        csrf_path="/u/bob/collections/new",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/bob/collections/guitars"

    rows = {r.user_id: r.slug for r in (await db.execute(select(Collection))).scalars().all()}
    assert rows[alice.id] == "guitars"
    assert rows[bob.id] == "guitars"


async def test_edit_collection(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    collection = Collection(
        user_id=user.id,
        name="Original name",
        slug="original",
        description="v1",
        is_public=False,
    )
    db.add(collection)
    await db.commit()

    await login(client, "alice")

    resp = await post_form(
        client,
        "/u/alice/collections/original",
        {
            "name": "New name",
            "slug": "original",
            "description": "Updated description",
        },
        csrf_path="/u/alice/collections/original/edit",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/collections/original"

    await db.refresh(collection)
    assert collection.name == "New name"
    assert collection.description == "Updated description"


async def test_toggle_is_public(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    collection = Collection(
        user_id=user.id, name="C", slug="c", is_public=False
    )
    db.add(collection)
    await db.commit()

    await login(client, "alice")

    await post_form(
        client,
        "/u/alice/collections/c",
        {"name": "C", "slug": "c", "description": "", "is_public": "1"},
        csrf_path="/u/alice/collections/c/edit",
    )
    await db.refresh(collection)
    assert collection.is_public is True

    # Unchecking omits the field → bool(None) False
    await post_form(
        client,
        "/u/alice/collections/c",
        {"name": "C", "slug": "c", "description": ""},
        csrf_path="/u/alice/collections/c/edit",
    )
    await db.refresh(collection)
    assert collection.is_public is False


async def test_delete_collection(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = await _make_project(db, user, title="Keep me", slug="keep-me")
    collection = Collection(user_id=user.id, name="C", slug="c")
    collection.projects = [project]
    db.add(collection)
    await db.commit()

    await login(client, "alice")

    resp = await post_form(
        client,
        "/u/alice/collections/c/delete",
        {},
        csrf_path="/u/alice/collections",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/collections"

    assert (await db.execute(select(Collection))).scalars().all() == []
    # Project row is untouched — only the membership is removed.
    remaining = (await db.execute(select(Project))).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].title == "Keep me"


async def test_detail_404_for_non_owner_when_private(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")

    collection = Collection(
        user_id=alice.id, name="Private", slug="private", is_public=False
    )
    db.add(collection)
    await db.commit()

    # Guest
    resp = await client.get("/u/alice/collections/private")
    assert resp.status_code == 404

    # Other logged-in user
    await login(client, "bob")
    resp = await client.get("/u/alice/collections/private")
    assert resp.status_code == 404


async def test_detail_hides_private_projects_for_guest(client, db):
    alice = await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    pub = await _make_project(
        db, alice, title="Public piece", slug="public-piece", is_public=True
    )
    priv = await _make_project(
        db, alice, title="Private piece", slug="private-piece", is_public=False
    )
    collection = Collection(
        user_id=alice.id, name="Mixed", slug="mixed", is_public=True
    )
    collection.projects = [pub, priv]
    db.add(collection)
    await db.commit()

    # Guest view — private project silently filtered out
    resp = await client.get("/u/alice/collections/mixed")
    assert resp.status_code == 200
    assert "Public piece" in resp.text
    assert "Private piece" not in resp.text

    # Owner view — sees both
    await login(client, "alice")
    resp = await client.get("/u/alice/collections/mixed")
    assert "Public piece" in resp.text
    assert "Private piece" in resp.text


async def test_add_project_to_collection_via_ajax(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = await _make_project(db, user, title="Thing", slug="thing")
    collection = Collection(user_id=user.id, name="C", slug="c")
    db.add(collection)
    await db.commit()
    await db.refresh(collection)

    await login(client, "alice")
    token = await csrf_token(client, "/u/alice/collections")
    resp = await client.post(
        "/u/alice/collections/c/projects",
        json={"project_id": str(project.id), "action": "add"},
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    assert resp.status_code == 204

    # Verify membership
    db.expunge_all()
    row = (
        await db.execute(
            select(Collection)
            .options(selectinload(Collection.projects))
            .where(Collection.id == collection.id)
        )
    ).scalar_one()
    assert [p.id for p in row.projects] == [project.id]


async def test_remove_project_from_collection_via_ajax(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = await _make_project(db, user, title="Thing", slug="thing")
    collection = Collection(user_id=user.id, name="C", slug="c")
    collection.projects = [project]
    db.add(collection)
    await db.commit()

    await login(client, "alice")
    token = await csrf_token(client, "/u/alice/collections")
    resp = await client.post(
        "/u/alice/collections/c/projects",
        json={"project_id": str(project.id), "action": "remove"},
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    assert resp.status_code == 204

    # Identity map holds pre-request state with expire_on_commit=False.
    db.expunge_all()
    row = (
        await db.execute(
            select(Collection)
            .options(selectinload(Collection.projects))
            .where(Collection.id == collection.id)
        )
    ).scalar_one()
    assert row.projects == []


async def test_add_project_not_owned_rejected(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    bobs_project = await _make_project(db, bob, title="Bob's", slug="bobs")
    collection = Collection(user_id=alice.id, name="C", slug="c")
    db.add(collection)
    await db.commit()

    await login(client, "alice")
    token = await csrf_token(client, "/u/alice/collections")
    resp = await client.post(
        "/u/alice/collections/c/projects",
        json={"project_id": str(bobs_project.id), "action": "add"},
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    assert resp.status_code == 404

    row = (
        await db.execute(
            select(Collection)
            .options(selectinload(Collection.projects))
            .where(Collection.id == collection.id)
        )
    ).scalar_one()
    assert row.projects == []


async def test_list_page_shows_public_collections_for_guests(client, db):
    alice = await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    db.add_all(
        [
            Collection(
                user_id=alice.id, name="Shared", slug="shared", is_public=True
            ),
            Collection(
                user_id=alice.id, name="Hidden", slug="hidden", is_public=False
            ),
        ]
    )
    await db.commit()

    resp = await client.get("/u/alice/collections")
    assert resp.status_code == 200
    assert "Shared" in resp.text
    assert "Hidden" not in resp.text


async def test_list_page_shows_all_collections_for_owner(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    db.add_all(
        [
            Collection(
                user_id=alice.id, name="Shared", slug="shared", is_public=True
            ),
            Collection(
                user_id=alice.id, name="Hidden", slug="hidden", is_public=False
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/collections")
    assert resp.status_code == 200
    assert "Shared" in resp.text
    assert "Hidden" in resp.text


async def test_profile_shows_public_collections(client, db):
    alice = await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    db.add_all(
        [
            Collection(
                user_id=alice.id,
                name="Public collection",
                slug="public-collection",
                is_public=True,
            ),
            Collection(
                user_id=alice.id,
                name="Secret stash",
                slug="secret-stash",
                is_public=False,
            ),
        ]
    )
    await db.commit()

    resp = await client.get("/u/alice")
    assert resp.status_code == 200
    assert "Public collection" in resp.text
    assert "Secret stash" not in resp.text


async def test_project_detail_modal_data_loaded(client, db):
    # The combobox modal pre-hydrates its state via two JSON script
    # blocks: `data-collections-options` (every collection owned by the
    # user) and `data-collections-initial` (ids this project is already
    # a member of). Assert both blocks render with the right data so
    # the client boots without needing a follow-up fetch.
    import json
    import re

    user = await make_user(db, email="alice@test.com", username="alice")
    project = await _make_project(db, user, title="Thing", slug="thing")
    in_collection = Collection(user_id=user.id, name="In", slug="in")
    in_collection.projects = [project]
    out_collection = Collection(user_id=user.id, name="Out", slug="out")
    db.add_all([in_collection, out_collection])
    await db.commit()
    # Refresh to pull the committed ids into the ORM for the assertion.
    await db.refresh(in_collection)
    await db.refresh(out_collection)

    await login(client, "alice")
    resp = await client.get("/u/alice/thing")
    assert resp.status_code == 200
    body = resp.text

    # Options catalog: both collections present with name + slug.
    opts_match = re.search(
        r'data-collections-options[^>]*>([^<]*)<',
        body,
    )
    assert opts_match is not None, "options JSON script not found"
    opts = json.loads(opts_match.group(1))
    names = {o["name"] for o in opts}
    assert names == {"In", "Out"}

    # Initial membership: only the "In" id.
    init_match = re.search(
        r'data-collections-initial[^>]*>([^<]*)<',
        body,
    )
    assert init_match is not None, "initial-membership JSON script not found"
    initial = set(json.loads(init_match.group(1)))
    assert str(in_collection.id) in initial
    assert str(out_collection.id) not in initial
