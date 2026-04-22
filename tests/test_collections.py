"""Tests for Collections — user-curated named groups of projects.

Mirrors test_projects.py patterns: access control, slug collisions,
visibility, and the AJAX toggle endpoint used by the add-to-collections
modal on the project detail page.
"""

import re

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


async def test_add_private_project_not_owned_rejected(client, db):
    # Cross-user private projects are invisible — adding one 404s, same
    # as any other "project doesn't exist for you" response.
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    bobs_project = await _make_project(
        db, bob, title="Bob's private", slug="bobs", is_public=False
    )
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


async def test_add_public_project_not_owned_succeeds(client, db):
    # Any visible project can be added — including another user's public.
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    bobs_project = await _make_project(
        db, bob, title="Bob's public", slug="bobs", is_public=True
    )
    collection = Collection(user_id=alice.id, name="C", slug="c")
    db.add(collection)
    await db.commit()
    await db.refresh(collection)

    await login(client, "alice")
    token = await csrf_token(client, "/u/alice/collections")
    resp = await client.post(
        "/u/alice/collections/c/projects",
        json={"project_id": str(bobs_project.id), "action": "add"},
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    assert resp.status_code == 204

    db.expunge_all()
    row = (
        await db.execute(
            select(Collection)
            .options(selectinload(Collection.projects))
            .where(Collection.id == collection.id)
        )
    ).scalar_one()
    assert [p.id for p in row.projects] == [bobs_project.id]


async def test_add_own_private_project_still_works(client, db):
    # The "own any visibility" path is preserved — owner scopes still allow
    # the owner to add their own private projects to their own collections.
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _make_project(
        db, alice, title="Mine private", slug="mine", is_public=False
    )
    collection = Collection(user_id=alice.id, name="C", slug="c")
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


async def test_detail_edit_toggle_renders_for_owner_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    alices = await _make_project(
        db, alice, title="AP", slug="ap", is_public=True
    )
    collection = Collection(
        user_id=alice.id, name="Faves", slug="faves", is_public=True
    )
    collection.projects = [alices]
    db.add(collection)
    await db.commit()

    # Owner sees the Edit toggle + per-card remove button.
    await login(client, "alice")
    resp = await client.get("/u/alice/collections/faves")
    assert resp.status_code == 200
    assert "data-collection-edit-toggle" in resp.text
    assert "data-collection-remove" in resp.text

    # Non-owner (logged in) doesn't see either.
    await login(client, "bob")
    resp = await client.get("/u/alice/collections/faves")
    assert resp.status_code == 200
    assert "data-collection-edit-toggle" not in resp.text
    assert "data-collection-remove" not in resp.text

    # Guest (logged out) doesn't see either.
    # Re-create a fresh client session so prior login cookies don't leak.
    from httpx import ASGITransport, AsyncClient
    from benchlog.main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
        follow_redirects=False,
    ) as guest:
        resp = await guest.get("/u/alice/collections/faves")
        assert resp.status_code == 200
        assert "data-collection-edit-toggle" not in resp.text
        assert "data-collection-remove" not in resp.text


async def test_detail_hides_cross_user_private_from_owner(client, db):
    # Alice has Bob's project in her collection; when Bob flips private,
    # Alice (the collection owner) shouldn't see the chip — she can't see
    # the project's detail page either, so the collection shouldn't pretend.
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    bobs = await _make_project(
        db, bob, title="BobsPiece", slug="bobs-piece", is_public=True
    )
    collection = Collection(
        user_id=alice.id, name="Mixed", slug="mixed", is_public=True
    )
    collection.projects = [bobs]
    db.add(collection)
    await db.commit()

    # Guest sees public project.
    resp = await client.get("/u/alice/collections/mixed")
    assert "BobsPiece" in resp.text

    # Bob flips it private — guest no longer sees it.
    bobs.is_public = False
    await db.commit()
    resp = await client.get("/u/alice/collections/mixed")
    assert "BobsPiece" not in resp.text

    # Alice (collection owner, not project owner) also can't see it.
    await login(client, "alice")
    resp = await client.get("/u/alice/collections/mixed")
    assert "BobsPiece" not in resp.text

    # Bob flips it back — reappears for Alice (and for guests again).
    bobs.is_public = True
    await db.commit()
    resp = await client.get("/u/alice/collections/mixed")
    assert "BobsPiece" in resp.text


async def test_list_page_count_filtered_by_viewer_visibility(client, db):
    # Alice's collection contains (1) her own private, (2) her own public,
    # (3) Bob's public.
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    alice_priv = await _make_project(
        db, alice, title="Alice private", slug="ap", is_public=False
    )
    alice_pub = await _make_project(
        db, alice, title="Alice public", slug="apub", is_public=True
    )
    bob_pub = await _make_project(
        db, bob, title="Bob public", slug="bpub", is_public=True
    )
    pub_c = Collection(
        user_id=alice.id, name="Showcase", slug="showcase", is_public=True
    )
    pub_c.projects = [alice_priv, alice_pub, bob_pub]
    priv_c = Collection(
        user_id=alice.id, name="Stash", slug="stash", is_public=False
    )
    priv_c.projects = [alice_priv]
    db.add_all([pub_c, priv_c])
    await db.commit()

    # Guest first — only the public collection; count is 2 (alice's
    # private filtered out; alice's public + bob's public still counted).
    resp = await client.get("/u/alice/collections")
    assert resp.status_code == 200
    assert "2 projects" in resp.text
    assert "Stash" not in resp.text

    # Bob flips his public project private — guest now sees 1.
    bob_pub.is_public = False
    await db.commit()
    resp = await client.get("/u/alice/collections")
    assert "1 project" in resp.text
    assert "2 projects" not in resp.text

    # Flip back so the owner view assertions have a stable world.
    bob_pub.is_public = True
    await db.commit()

    # Owner view — sees both her collections. Showcase has all three of
    # her visible projects (own any visibility + bob's public).
    await login(client, "alice")
    resp = await client.get("/u/alice/collections")
    assert resp.status_code == 200
    assert "3 projects" in resp.text
    assert "Stash" in resp.text
    assert "1 project" in resp.text

    # Bob flips private again — owner count for Showcase drops to 2
    # (the two alice-owned projects; bob's membership row still exists
    # but she can't see it either).
    bob_pub.is_public = False
    await db.commit()
    resp = await client.get("/u/alice/collections")
    assert "2 projects" in resp.text
    assert "3 projects" not in resp.text


async def test_project_detail_shows_featured_in_public_collections(client, db):
    # Alice owns a public project; Bob and Carol both add it to their
    # public collections. Any visitor to the project page should see a
    # "Featured in collections" section linking to Bob's and Carol's
    # collections, but NOT to Bob's private collection. The viewer's own
    # collections are excluded (already shown as chips at the top).
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    carol = await make_user(db, email="carol@test.com", username="carol")
    project = await _make_project(
        db, alice, title="AP", slug="ap", is_public=True
    )
    bob_pub = Collection(
        user_id=bob.id, name="BobsFaves", slug="bobs-faves", is_public=True
    )
    bob_priv = Collection(
        user_id=bob.id, name="BobsStash", slug="bobs-stash", is_public=False
    )
    carol_pub = Collection(
        user_id=carol.id, name="CarolsPicks", slug="carols-picks", is_public=True
    )
    bob_pub.projects = [project]
    bob_priv.projects = [project]
    carol_pub.projects = [project]
    db.add_all([bob_pub, bob_priv, carol_pub])
    await db.commit()

    # Guest: sees public collections from both Bob and Carol, not the private one.
    resp = await client.get("/u/alice/ap")
    assert resp.status_code == 200
    assert "BobsFaves" in resp.text
    assert "CarolsPicks" in resp.text
    assert "BobsStash" not in resp.text

    # Bob viewing Alice's project: "BobsFaves" still appears at the top
    # (chip row shows his own collections that include this project), but
    # the "Featured in collections" discovery section must exclude it to
    # avoid duplication. Scope the assertion to just that section.
    await login(client, "bob")
    resp = await client.get("/u/alice/ap")
    m = re.search(
        r'data-featured-in-collections[^>]*>(.*?)</section>',
        resp.text,
        re.DOTALL,
    )
    assert m is not None, "featured-in-collections section should render"
    featured = m.group(1)
    assert "BobsFaves" not in featured
    assert "CarolsPicks" in featured


async def test_private_project_hidden_from_its_owner_in_other_collection(client, db):
    # The whole point of the "in someone else's collection, show only public"
    # rule: Alice shouldn't see her own private project surfaced in Bob's
    # collection, because that would make her wonder if others can see it
    # too. In her own collections, her private stash is hers to see.
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    alices = await _make_project(
        db, alice, title="AlicesPiece", slug="alices-piece", is_public=True
    )
    bobs_collection = Collection(
        user_id=bob.id, name="Faves", slug="faves", is_public=True
    )
    bobs_collection.projects = [alices]
    db.add(bobs_collection)
    await db.commit()

    # Baseline: Alice logged in sees her public project in Bob's collection.
    await login(client, "alice")
    resp = await client.get("/u/bob/collections/faves")
    assert "AlicesPiece" in resp.text

    # Alice flips her own project private. In Bob's collection she should
    # no longer see it (same as every other viewer).
    alices.is_public = False
    await db.commit()
    resp = await client.get("/u/bob/collections/faves")
    assert "AlicesPiece" not in resp.text


async def test_picker_rendered_for_logged_in_non_owner(client, db):
    # Viewing someone else's public project as a logged-in user — the
    # add-to-collections picker should render and target the viewer's
    # namespace, not the project owner's.
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    await _make_project(
        db, bob, title="BobsPiece", slug="bobs-piece", is_public=True
    )
    # Alice has one existing collection to populate the picker's options.
    db.add(Collection(user_id=alice.id, name="Inspiration", slug="inspiration"))
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/bob/bobs-piece")
    assert resp.status_code == 200
    body = resp.text
    assert "data-collections-picker" in body
    assert "data-collections-modal" in body
    # URLs target Alice (the viewer), NOT Bob (the project owner).
    assert 'data-toggle-url-prefix="/u/alice/collections/"' in body
    assert 'data-create-url="/u/alice/collections"' in body
    # Alice's collection appears in the options catalog.
    assert "Inspiration" in body


async def test_picker_not_rendered_for_guests(client, db):
    bob = await make_user(db, email="bob@test.com", username="bob")
    await _make_project(
        db, bob, title="BobsPiece", slug="bobs-piece", is_public=True
    )

    resp = await client.get("/u/bob/bobs-piece")
    assert resp.status_code == 200
    assert "data-collections-picker" not in resp.text
    assert "data-collections-modal" not in resp.text


async def test_non_owner_can_add_visible_project_end_to_end(client, db):
    # Full flow: Alice adds Bob's public project to Alice's collection
    # via the JSON toggle endpoint, then opens her collection and sees it.
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    bobs = await _make_project(
        db, bob, title="BobsPiece", slug="bobs-piece", is_public=True
    )
    collection = Collection(
        user_id=alice.id, name="Inspo", slug="inspo", is_public=True
    )
    db.add(collection)
    await db.commit()

    await login(client, "alice")
    token = await csrf_token(client, "/u/alice/collections")
    resp = await client.post(
        "/u/alice/collections/inspo/projects",
        json={"project_id": str(bobs.id), "action": "add"},
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    assert resp.status_code == 204

    resp = await client.get("/u/alice/collections/inspo")
    assert "BobsPiece" in resp.text
