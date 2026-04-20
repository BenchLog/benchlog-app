"""Tests for the /explore filter sidebar — multi-status, multi-tag, and
visibility hardening (Explore always filters to public regardless of the
?visibility= param).
"""

import re

from benchlog.models import Project, ProjectStatus
from tests.conftest import login, make_user, post_form


async def test_explore_filter_multiple_statuses(client, db):
    alice = await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    db.add_all(
        [
            Project(
                user_id=alice.id,
                title="Idea Pub",
                slug="idea-pub",
                status=ProjectStatus.idea,
                is_public=True,
            ),
            Project(
                user_id=alice.id,
                title="Doing Pub",
                slug="doing-pub",
                status=ProjectStatus.in_progress,
                is_public=True,
            ),
            Project(
                user_id=alice.id,
                title="Done Pub",
                slug="done-pub",
                status=ProjectStatus.completed,
                is_public=True,
            ),
        ]
    )
    await db.commit()

    resp = await client.get("/explore?status=idea&status=completed")
    assert "Idea Pub" in resp.text
    assert "Done Pub" in resp.text
    assert "Doing Pub" not in resp.text


async def test_explore_filter_multiple_tags_requires_all(client, db):
    await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    await login(client, "alice")

    for title, tags in [
        ("Pub A B", "a, b"),
        ("Pub A C", "a, c"),
        ("Pub A only", "a"),
    ]:
        await post_form(
            client,
            "/projects",
            {
                "title": title,
                "description": "",
                "status": "in_progress",
                "tags": tags,
                "is_public": "1",
            },
            csrf_path="/projects/new",
        )

    # Logged-out GET works too, but we reuse the logged-in client.
    resp = await client.get("/explore?tag=a&tag=b")
    assert "Pub A B" in resp.text
    assert "Pub A C" not in resp.text
    assert "Pub A only" not in resp.text


async def test_explore_filter_tag_mode_any_on_public(client, db):
    # The OR mode motivating use case: spelling variants of the same topic.
    await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    await login(client, "alice")

    for title, tags in [
        ("Print Variant", "3d-printing"),
        ("Print Typo", "3d-printed"),
        ("Not Print", "woodworking"),
    ]:
        await post_form(
            client,
            "/projects",
            {
                "title": title,
                "description": "",
                "status": "in_progress",
                "tags": tags,
                "is_public": "1",
            },
            csrf_path="/projects/new",
        )

    resp = await client.get(
        "/explore?tag=3d-printing&tag=3d-printed&tag_mode=any"
    )
    assert "Print Variant" in resp.text
    assert "Print Typo" in resp.text
    assert "Not Print" not in resp.text


async def test_explore_known_tags_only_from_public_projects(client, db):
    alice = await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    await login(client, "alice")

    # A public project with `public-tag`
    await post_form(
        client,
        "/projects",
        {
            "title": "Pub shed",
            "description": "",
            "status": "in_progress",
            "tags": "public-tag",
            "is_public": "1",
        },
        csrf_path="/projects/new",
    )
    # A private project with `private-tag` — should NOT surface in Explore's
    # autocomplete list.
    await post_form(
        client,
        "/projects",
        {
            "title": "Priv shed",
            "description": "",
            "status": "in_progress",
            "tags": "private-tag",
        },
        csrf_path="/projects/new",
    )

    # Hit /explore as a guest to mirror the actual autocomplete surface.
    resp = await client.get("/explore")
    assert resp.status_code == 200

    known_match = re.search(r'data-known-tags="([^"]*)"', resp.text)
    assert known_match is not None
    known = set(known_match.group(1).split())
    assert "public-tag" in known
    assert "private-tag" not in known

    # Sanity: alice's project-form combobox SHOULD include the private tag
    # (the user's own vocabulary is her entire vocabulary).
    resp = await client.get("/projects/new")
    known_match = re.search(r'data-known-tags="([^"]*)"', resp.text)
    assert known_match is not None
    alice_known = set(known_match.group(1).split())
    assert "private-tag" in alice_known
    assert "public-tag" in alice_known

    # Keeps type-checker + ruff happy re. unused var
    assert alice.username == "alice"


async def test_explore_visibility_param_is_ignored(client, db):
    alice = await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    db.add_all(
        [
            Project(
                user_id=alice.id,
                title="Pub shelf",
                slug="pub-shelf",
                status=ProjectStatus.in_progress,
                is_public=True,
            ),
            Project(
                user_id=alice.id,
                title="Priv shelf",
                slug="priv-shelf",
                status=ProjectStatus.in_progress,
                is_public=False,
            ),
        ]
    )
    await db.commit()

    # Even when a curious visitor hand-edits ?visibility=private into the URL,
    # Explore keeps its public-only contract.
    resp = await client.get("/explore?visibility=private")
    assert resp.status_code == 200
    assert "Pub shelf" in resp.text
    assert "Priv shelf" not in resp.text
