"""Tests for inter-project links (ProjectRelation).

Covers the helper-layer guarantees (owner gate, visibility, self / duplicate
rejection, cascade) and the routes (add / remove / search) that back the
add-relation modal on the project detail page.
"""

import pytest
from sqlalchemy import select

from benchlog.models import Project, ProjectRelation, ProjectStatus, RelationType
from benchlog.project_relations import (
    DuplicateRelationError,
    RelationError,
    add_relation,
    search_linkable_projects,
)
from tests.conftest import csrf_token, login, make_user


async def _make_project(
    db, user, *, title, slug, is_public=False, status=ProjectStatus.idea
):
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


async def _add_url(client, username, slug):
    return f"/u/{username}/{slug}/relations"


# ---------- add ---------- #


async def test_add_relation_owner_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    alices_src = await _make_project(db, alice, title="A", slug="a")
    target = await _make_project(db, bob, title="B", slug="b", is_public=True)

    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        f"/u/alice/{alices_src.slug}/relations",
        json={"target_id": str(target.id), "type": "inspired_by"},
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    assert resp.status_code == 404

    rows = (await db.execute(select(ProjectRelation))).scalars().all()
    assert rows == []


async def test_add_relation_to_own_private_project_allowed(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, title="Src", slug="src")
    target = await _make_project(
        db, alice, title="Priv", slug="priv", is_public=False
    )

    await login(client, "alice")
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        await _add_url(client, "alice", src.slug),
        json={"target_id": str(target.id), "type": "depends_on"},
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["target_title"] == "Priv"
    assert body["type"] == "depends_on"


async def test_add_relation_to_other_users_public_project_allowed(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    src = await _make_project(db, alice, title="A", slug="a")
    pub = await _make_project(db, bob, title="Pub", slug="pub", is_public=True)

    await login(client, "alice")
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        await _add_url(client, "alice", src.slug),
        json={"target_id": str(pub.id), "type": "inspired_by"},
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    assert resp.status_code == 201
    assert resp.json()["target_username"] == "bob"


async def test_add_relation_to_other_users_private_project_rejected(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    src = await _make_project(db, alice, title="A", slug="a")
    priv = await _make_project(
        db, bob, title="Priv", slug="priv", is_public=False
    )

    await login(client, "alice")
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        await _add_url(client, "alice", src.slug),
        json={"target_id": str(priv.id), "type": "inspired_by"},
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    assert resp.status_code == 400
    # No row should have been created.
    assert (await db.execute(select(ProjectRelation))).scalars().all() == []


async def test_add_relation_self_reference_rejected(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, title="A", slug="a")

    await login(client, "alice")
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        await _add_url(client, "alice", src.slug),
        json={"target_id": str(src.id), "type": "related_to"},
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    assert resp.status_code == 400


async def test_add_relation_rejects_fork_of_from_user_route(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, title="A", slug="a")
    tgt = await _make_project(db, alice, title="B", slug="b")

    await login(client, "alice")
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        await _add_url(client, "alice", src.slug),
        json={"target_id": str(tgt.id), "type": "fork_of"},
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    assert resp.status_code == 400

    # But the helper allows it when allow_system_types is set — used by
    # the Forks feature when creating a new fork server-side.
    await add_relation(
        db, src, tgt.id, RelationType.fork_of, alice, allow_system_types=True
    )
    await db.commit()
    rows = (await db.execute(select(ProjectRelation))).scalars().all()
    assert len(rows) == 1
    assert rows[0].relation_type == RelationType.fork_of


async def test_add_relation_duplicate_triple_rejected(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, title="A", slug="a")
    tgt = await _make_project(db, alice, title="B", slug="b")

    await login(client, "alice")
    token = await csrf_token(client, "/projects")
    first = await client.post(
        await _add_url(client, "alice", src.slug),
        json={"target_id": str(tgt.id), "type": "inspired_by"},
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    assert first.status_code == 201

    dup = await client.post(
        await _add_url(client, "alice", src.slug),
        json={"target_id": str(tgt.id), "type": "inspired_by"},
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    assert dup.status_code == 409


async def test_add_relation_different_type_to_same_target_allowed(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, title="A", slug="a")
    tgt = await _make_project(db, alice, title="B", slug="b")

    await login(client, "alice")
    token = await csrf_token(client, "/projects")
    r1 = await client.post(
        await _add_url(client, "alice", src.slug),
        json={"target_id": str(tgt.id), "type": "inspired_by"},
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    r2 = await client.post(
        await _add_url(client, "alice", src.slug),
        json={"target_id": str(tgt.id), "type": "depends_on"},
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    rows = (await db.execute(select(ProjectRelation))).scalars().all()
    assert len(rows) == 2


# ---------- remove ---------- #


async def test_remove_relation_owner_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    src = await _make_project(db, alice, title="A", slug="a", is_public=True)
    tgt = await _make_project(db, alice, title="B", slug="b", is_public=True)

    rel = await add_relation(db, src, tgt.id, RelationType.related_to, alice)
    await db.commit()

    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        f"/u/alice/{src.slug}/relations/{rel.id}/delete",
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    assert resp.status_code == 404

    rows = (await db.execute(select(ProjectRelation))).scalars().all()
    assert len(rows) == 1


async def test_remove_relation_happy_path(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, title="A", slug="a")
    tgt = await _make_project(db, alice, title="B", slug="b")

    rel = await add_relation(db, src, tgt.id, RelationType.related_to, alice)
    await db.commit()

    await login(client, "alice")
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        f"/u/alice/{src.slug}/relations/{rel.id}/delete",
        headers={"Accept": "application/json", "X-CSRF-Token": token},
    )
    assert resp.status_code == 204

    db.expunge_all()
    rows = (await db.execute(select(ProjectRelation))).scalars().all()
    assert rows == []


# ---------- cascades ---------- #


async def test_deleting_source_project_cascades_relations(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, title="A", slug="a")
    tgt = await _make_project(db, alice, title="B", slug="b")
    await add_relation(db, src, tgt.id, RelationType.related_to, alice)
    await db.commit()

    await db.delete(src)
    await db.commit()

    rows = (await db.execute(select(ProjectRelation))).scalars().all()
    assert rows == []


async def test_deleting_target_project_cascades_relations(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, title="A", slug="a")
    tgt = await _make_project(db, alice, title="B", slug="b")
    await add_relation(db, src, tgt.id, RelationType.related_to, alice)
    await db.commit()

    await db.delete(tgt)
    await db.commit()

    rows = (await db.execute(select(ProjectRelation))).scalars().all()
    assert rows == []


# ---------- detail-page visibility ---------- #


async def test_detail_outgoing_hides_private_targets_from_guest(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    # Source is public so guests can see the detail page at all.
    src = await _make_project(db, alice, title="Src", slug="src", is_public=True)
    pub = await _make_project(
        db, alice, title="Pub target", slug="pub-target", is_public=True
    )
    priv = await _make_project(
        db, alice, title="Priv target", slug="priv-target", is_public=False
    )
    await add_relation(db, src, pub.id, RelationType.inspired_by, alice)
    await add_relation(db, src, priv.id, RelationType.related_to, alice)
    await db.commit()

    # Guest: private target hidden.
    resp = await client.get("/u/alice/src")
    assert resp.status_code == 200
    assert "Pub target" in resp.text
    assert "Priv target" not in resp.text

    # Owner: sees both.
    await login(client, "alice")
    resp = await client.get("/u/alice/src")
    assert "Pub target" in resp.text
    assert "Priv target" in resp.text


async def test_detail_incoming_hides_private_source_from_guest(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    # Target of the incoming relation — must be public for a guest to
    # even load the detail page.
    tgt = await _make_project(
        db, alice, title="Target", slug="target", is_public=True
    )
    # Public incoming source — should show up in "Referenced by".
    public_src = await _make_project(
        db, bob, title="Public source", slug="public-src", is_public=True
    )
    # Private incoming source (Bob's) — should be hidden from guests.
    private_src = await _make_project(
        db, bob, title="Private source", slug="private-src", is_public=False
    )
    await add_relation(
        db, public_src, tgt.id, RelationType.inspired_by, bob
    )
    # Bob linking from his private project — allowed (his own project).
    await add_relation(
        db,
        private_src,
        tgt.id,
        RelationType.inspired_by,
        bob,
    )
    await db.commit()

    resp = await client.get("/u/alice/target")
    assert resp.status_code == 200
    assert "Public source" in resp.text
    assert "Private source" not in resp.text


async def test_detail_shows_relation_chip_for_viewable_target(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, title="Src", slug="src", is_public=True)
    tgt = await _make_project(
        db, alice, title="Target project", slug="target-proj", is_public=True
    )
    await add_relation(db, src, tgt.id, RelationType.depends_on, alice)
    await db.commit()

    resp = await client.get("/u/alice/src")
    assert resp.status_code == 200
    assert "Target project" in resp.text
    assert "Depends on" in resp.text


# ---------- search ---------- #


async def test_search_excludes_self(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, title="Source", slug="source")
    other = await _make_project(
        db, alice, title="Sidecar", slug="sidecar", is_public=True
    )

    await login(client, "alice")
    resp = await client.get(f"/u/alice/{src.slug}/relations/search?q=")
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()["results"]]
    assert str(src.id) not in ids
    assert str(other.id) in ids


async def test_search_filters_private_others_projects(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    src = await _make_project(db, alice, title="Source", slug="source")
    bob_pub = await _make_project(
        db, bob, title="Bob public", slug="bob-pub", is_public=True
    )
    bob_priv = await _make_project(
        db, bob, title="Bob private", slug="bob-priv", is_public=False
    )

    await login(client, "alice")
    resp = await client.get(f"/u/alice/{src.slug}/relations/search?q=")
    ids = [r["id"] for r in resp.json()["results"]]
    assert str(bob_pub.id) in ids
    assert str(bob_priv.id) not in ids


# ---------- helper-level smoke tests ---------- #


async def test_helper_rejects_non_owner_actor(db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    src = await _make_project(db, alice, title="A", slug="a")
    tgt = await _make_project(db, alice, title="B", slug="b")

    with pytest.raises(RelationError):
        await add_relation(db, src, tgt.id, RelationType.related_to, bob)


async def test_helper_raises_duplicate_error(db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, title="A", slug="a")
    tgt = await _make_project(db, alice, title="B", slug="b")

    await add_relation(db, src, tgt.id, RelationType.inspired_by, alice)
    await db.commit()

    with pytest.raises(DuplicateRelationError):
        await add_relation(db, src, tgt.id, RelationType.inspired_by, alice)


async def test_search_helper_prefix_matches(db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, title="Source", slug="source")
    await _make_project(
        db, alice, title="Flowire router", slug="flowire", is_public=True
    )
    await _make_project(db, alice, title="Other thing", slug="other")

    matches = await search_linkable_projects(
        db, alice, "flowi", exclude_project_id=src.id
    )
    titles = [p.title for p in matches]
    assert "Flowire router" in titles
    assert "Other thing" not in titles


async def test_search_helper_ignores_description_matches(db):
    # Relations picker is title-only — matching on description text (e.g.
    # a project titled "Other" whose description mentions "flowire") would
    # be a surprising result in a picker combobox.
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, title="Source", slug="source")
    decoy = await _make_project(
        db, alice, title="Unrelated", slug="unrelated", is_public=True
    )
    decoy.description = "This project mentions flowire in the body text."
    await db.commit()

    matches = await search_linkable_projects(
        db, alice, "flowire", exclude_project_id=src.id
    )
    titles = [p.title for p in matches]
    assert "Unrelated" not in titles
