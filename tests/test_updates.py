"""Tests for project updates — CRUD, ordering, visibility, markdown render."""

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from benchlog.models import Project, ProjectStatus, ProjectUpdate
from tests.conftest import login, make_user, post_form


# ---------- create ---------- #


async def test_owner_creates_update_and_redirects_to_project(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Bench",
            slug="bench",
            status=ProjectStatus.in_progress,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        "/u/alice/bench/updates",
        {"title": "Day 1", "content": "Glued the tenons."},
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 302
    # Post-create lands on the Updates tab anchored to the new entry.
    assert "/u/alice/bench/updates#update-" in resp.headers["location"]

    update = (
        await db.execute(select(ProjectUpdate))
    ).scalar_one()
    assert update.title == "Day 1"
    assert update.content == "Glued the tenons."


async def test_create_update_requires_content(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Bench",
            slug="bench",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        "/u/alice/bench/updates",
        {"title": "Stub", "content": "   "},
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 400
    assert "Content is required." in resp.text

    remaining = (await db.execute(select(ProjectUpdate))).scalars().all()
    assert remaining == []


async def test_create_update_allows_blank_title(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Bench",
            slug="bench",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")
    await post_form(
        client,
        "/u/alice/bench/updates",
        {"title": "", "content": "Untitled but substantive."},
        csrf_path="/u/alice/bench",
    )
    update = (await db.execute(select(ProjectUpdate))).scalar_one()
    assert update.title is None
    assert update.content == "Untitled but substantive."


# ---------- feed ordering on project detail ---------- #


async def test_updates_feed_is_newest_first(client, db):
    from datetime import datetime, timedelta, timezone

    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    # Explicit distinct timestamps — server_default=now() collapses to a
    # single value within one transaction, so bulk inserts would tie.
    base = datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc)
    db.add_all(
        [
            ProjectUpdate(
                project_id=project.id, content="marker-alpha", created_at=base
            ),
            ProjectUpdate(
                project_id=project.id,
                content="marker-bravo",
                created_at=base + timedelta(hours=1),
            ),
            ProjectUpdate(
                project_id=project.id,
                content="marker-charlie",
                created_at=base + timedelta(hours=2),
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/bench/updates")
    body = resp.text
    # Newest (charlie) comes before older (bravo, alpha).
    assert (
        body.index("marker-charlie")
        < body.index("marker-bravo")
        < body.index("marker-alpha")
    )


# ---------- edit ---------- #


async def test_owner_can_edit_update(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    update = ProjectUpdate(project_id=project.id, title="Initial", content="draft")
    db.add(update)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/bench/updates/{update.id}",
        {"title": "Revised", "content": "final"},
        csrf_path=f"/u/alice/bench/updates/{update.id}/edit",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/u/alice/bench/updates/{update.id}"

    await db.refresh(update)
    assert update.title == "Revised"
    assert update.content == "final"


# ---------- delete ---------- #


async def test_owner_can_delete_update(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    update = ProjectUpdate(project_id=project.id, content="soon gone")
    db.add(update)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/bench/updates/{update.id}/delete",
        {},
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 302
    # Deleted → land back on the Updates tab (where delete was invoked from).
    assert resp.headers["location"] == "/u/alice/bench/updates"

    remaining = (await db.execute(select(ProjectUpdate))).scalars().all()
    assert remaining == []


# ---------- visibility & access control ---------- #


async def test_non_owner_cannot_edit_or_delete_update(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")

    project = Project(
        user_id=alice.id,
        title="Alice Public",
        slug="alice-public",
        status=ProjectStatus.in_progress,
        is_public=True,
    )
    db.add(project)
    await db.flush()
    update = ProjectUpdate(project_id=project.id, content="public update")
    db.add(update)
    await db.commit()

    await login(client, "bob")

    # Edit form hits 404 because URL username != session username
    resp = await client.get(f"/u/alice/alice-public/updates/{update.id}/edit")
    assert resp.status_code == 404

    # Update POST rejected
    resp = await post_form(
        client,
        f"/u/alice/alice-public/updates/{update.id}",
        {"title": "pwned", "content": "gotcha"},
        csrf_path="/projects",
    )
    assert resp.status_code == 404

    # Delete rejected
    resp = await post_form(
        client,
        f"/u/alice/alice-public/updates/{update.id}/delete",
        {},
        csrf_path="/projects",
    )
    assert resp.status_code == 404

    await db.refresh(update)
    assert update.content == "public update"


async def test_guest_can_view_update_on_public_project(client, db):
    user = await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    project = Project(
        user_id=user.id,
        title="Public Bench",
        slug="public-bench",
        status=ProjectStatus.in_progress,
        is_public=True,
    )
    db.add(project)
    await db.flush()
    # Must be flagged public — private is the update-level default.
    update = ProjectUpdate(
        project_id=project.id,
        title="Finished",
        content="Glued tenons.",
        is_public=True,
    )
    db.add(update)
    await db.commit()

    resp = await client.get(f"/u/alice/public-bench/updates/{update.id}")
    assert resp.status_code == 200
    assert "Finished" in resp.text
    assert "Glued tenons." in resp.text
    assert "by Alice" in resp.text
    # No edit affordance for guests
    assert f"/u/alice/public-bench/updates/{update.id}/edit" not in resp.text


async def test_guest_cannot_view_update_on_private_project(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Private Bench",
        slug="private-bench",
        status=ProjectStatus.idea,
        is_public=False,
    )
    db.add(project)
    await db.flush()
    update = ProjectUpdate(project_id=project.id, content="shh")
    db.add(update)
    await db.commit()

    resp = await client.get(f"/u/alice/private-bench/updates/{update.id}")
    assert resp.status_code == 404


async def test_guest_post_to_updates_redirects_to_login(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Bench",
            slug="bench",
            status=ProjectStatus.idea,
            is_public=True,
        )
    )
    await db.commit()

    resp = await client.post(
        "/u/alice/bench/updates",
        data={"title": "x", "content": "y"},
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


async def test_guest_new_update_form_redirects_to_login(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Bench",
            slug="bench",
            status=ProjectStatus.idea,
            is_public=True,
        )
    )
    await db.commit()

    resp = await client.get("/u/alice/bench/updates/new")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


# ---------- rendering ---------- #


async def test_update_markdown_renders_as_html_on_feed(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    update = ProjectUpdate(
        project_id=project.id,
        content="## Heading\n\n- [x] done\n- [ ] todo",
    )
    db.add(update)
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/bench/updates")
    # Heading rendered, task list markers present
    assert "<h2>Heading</h2>" in resp.text
    assert "task-list-item" in resp.text


async def test_update_escapes_raw_html_in_input(client, db):
    """markdown-it 'gfm-like' preset has html=false, so raw <script>
    tags in user input get escaped, not executed."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Bench",
            slug="bench",
            status=ProjectStatus.in_progress,
        )
    )
    await db.commit()

    await login(client, "alice")
    await post_form(
        client,
        "/u/alice/bench/updates",
        {"title": "", "content": "<script>alert('x')</script>\n\nsafe body"},
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/updates")
    # Literal <script> tag must not appear unescaped
    assert "<script>alert('x')</script>" not in resp.text
    # Escaped version is fine
    assert "&lt;script&gt;" in resp.text
    # Rest of the body still renders
    assert "safe body" in resp.text


# ---------- cascade ---------- #


async def test_deleting_project_cascades_to_updates(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    db.add(ProjectUpdate(project_id=project.id, content="a"))
    db.add(ProjectUpdate(project_id=project.id, content="b"))
    await db.commit()

    await login(client, "alice")
    await post_form(
        client,
        "/u/alice/bench/delete",
        {},
        csrf_path="/projects",
    )

    remaining = (await db.execute(select(ProjectUpdate))).scalars().all()
    assert remaining == []


# ---------- project tabs ---------- #


async def test_project_pages_render_tab_bar_with_correct_active_tab(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    update = ProjectUpdate(project_id=project.id, content="body")
    db.add(update)
    await db.commit()

    await login(client, "alice")

    # Overview (root) — Overview tab is current, Updates tab is not.
    resp = await client.get("/u/alice/bench")
    assert resp.status_code == 200
    body = resp.text
    assert 'href="/u/alice/bench"' in body
    assert 'href="/u/alice/bench/updates"' in body
    overview_anchor = body[body.index('href="/u/alice/bench"') : body.index('Overview') + len('Overview')]
    updates_anchor = body[body.index('href="/u/alice/bench/updates"') : body.index('Updates') + len('Updates')]
    assert 'aria-current="page"' in overview_anchor
    assert 'aria-current="page"' not in updates_anchor

    # /updates — Updates tab active.
    resp = await client.get("/u/alice/bench/updates")
    assert resp.status_code == 200
    body = resp.text
    # Find the Updates nav anchor and verify it's marked current
    updates_anchor_start = body.index('href="/u/alice/bench/updates"')
    updates_anchor_block = body[updates_anchor_start:updates_anchor_start + 300]
    assert 'aria-current="page"' in updates_anchor_block

    # Single update page — still under Updates tab.
    resp = await client.get(f"/u/alice/bench/updates/{update.id}")
    assert resp.status_code == 200
    body = resp.text
    updates_anchor_start = body.index('href="/u/alice/bench/updates"')
    updates_anchor_block = body[updates_anchor_start:updates_anchor_start + 300]
    assert 'aria-current="page"' in updates_anchor_block


async def test_updates_tab_shows_full_feed(client, db):
    from datetime import datetime, timedelta, timezone

    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    base = datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        db.add(
            ProjectUpdate(
                project_id=project.id,
                content=f"entry-{i}",
                created_at=base + timedelta(hours=i),
            )
        )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/bench/updates")
    assert resp.status_code == 200
    # All 5 appear on the Updates tab
    for i in range(5):
        assert f"entry-{i}" in resp.text


async def test_overview_shows_no_updates_feed(client, db):
    """Overview is description-only; the feed lives on the Updates tab."""
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        description="A shop stool.",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    db.add(ProjectUpdate(project_id=project.id, content="update-body"))
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/bench")
    assert resp.status_code == 200
    # Description is shown
    assert "A shop stool." in resp.text
    # Update content does not appear on overview
    assert "update-body" not in resp.text


async def test_guest_can_load_updates_tab_on_public_project(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
        is_public=True,
    )
    db.add(project)
    await db.flush()
    # Update must itself be flagged public for guests to see it.
    db.add(
        ProjectUpdate(
            project_id=project.id, content="open update", is_public=True
        )
    )
    await db.commit()

    resp = await client.get("/u/alice/bench/updates")
    assert resp.status_code == 200
    assert "open update" in resp.text


async def test_guest_updates_tab_404s_on_private_project(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Bench",
            slug="bench",
            status=ProjectStatus.idea,
            is_public=False,
        )
    )
    await db.commit()

    resp = await client.get("/u/alice/bench/updates")
    assert resp.status_code == 404


# ---------- per-update visibility ---------- #


async def test_new_update_defaults_to_private(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Bench",
            slug="bench",
            status=ProjectStatus.in_progress,
        )
    )
    await db.commit()

    await login(client, "alice")
    await post_form(
        client,
        "/u/alice/bench/updates",
        {"title": "", "content": "quiet note"},
        csrf_path="/u/alice/bench",
    )
    update = (await db.execute(select(ProjectUpdate))).scalar_one()
    assert update.is_public is False


async def test_owner_can_flip_update_public_via_form(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Bench",
            slug="bench",
            status=ProjectStatus.in_progress,
        )
    )
    await db.commit()

    await login(client, "alice")
    # Create with is_public checked.
    await post_form(
        client,
        "/u/alice/bench/updates",
        {"title": "", "content": "visible from the start", "is_public": "1"},
        csrf_path="/u/alice/bench",
    )
    update = (await db.execute(select(ProjectUpdate))).scalar_one()
    assert update.is_public is True

    # Edit with the box unchecked — form omission means False.
    await post_form(
        client,
        f"/u/alice/bench/updates/{update.id}",
        {"title": "", "content": "visible from the start"},
        csrf_path=f"/u/alice/bench/updates/{update.id}/edit",
    )
    await db.refresh(update)
    assert update.is_public is False


async def test_feed_filters_private_updates_for_non_owners(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    project = Project(
        user_id=user.id,
        title="Public Bench",
        slug="public-bench",
        status=ProjectStatus.in_progress,
        is_public=True,
    )
    db.add(project)
    await db.flush()
    db.add_all(
        [
            ProjectUpdate(
                project_id=project.id, content="alice-public", is_public=True
            ),
            ProjectUpdate(
                project_id=project.id, content="alice-private", is_public=False
            ),
        ]
    )
    await db.commit()

    # Guest: only public updates
    resp = await client.get("/u/alice/public-bench/updates")
    assert "alice-public" in resp.text
    assert "alice-private" not in resp.text

    # Another logged-in user (bob): still filtered — not the owner
    await login(client, "bob")
    resp = await client.get("/u/alice/public-bench/updates")
    assert "alice-public" in resp.text
    assert "alice-private" not in resp.text


async def test_feed_shows_private_updates_to_owner(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Public Bench",
        slug="public-bench",
        status=ProjectStatus.in_progress,
        is_public=True,
    )
    db.add(project)
    await db.flush()
    db.add_all(
        [
            ProjectUpdate(
                project_id=project.id, content="alice-public", is_public=True
            ),
            ProjectUpdate(
                project_id=project.id, content="alice-private", is_public=False
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/public-bench/updates")
    assert "alice-public" in resp.text
    assert "alice-private" in resp.text


async def test_private_update_permalink_404s_for_non_owner(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    project = Project(
        user_id=user.id,
        title="Public Bench",
        slug="public-bench",
        status=ProjectStatus.in_progress,
        is_public=True,
    )
    db.add(project)
    await db.flush()
    update = ProjectUpdate(
        project_id=project.id, content="draft thought", is_public=False
    )
    db.add(update)
    await db.commit()

    # Guest
    resp = await client.get(f"/u/alice/public-bench/updates/{update.id}")
    assert resp.status_code == 404

    # Logged-in non-owner
    await login(client, "bob")
    resp = await client.get(f"/u/alice/public-bench/updates/{update.id}")
    assert resp.status_code == 404


async def test_private_project_hides_even_public_updates_from_non_owners(
    client, db,
):
    """Project visibility dominates: a public update on a private project
    stays hidden to everyone but the owner."""
    user = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    project = Project(
        user_id=user.id,
        title="Private Bench",
        slug="private-bench",
        status=ProjectStatus.idea,
        is_public=False,
    )
    db.add(project)
    await db.flush()
    update = ProjectUpdate(
        project_id=project.id, content="would be public", is_public=True
    )
    db.add(update)
    await db.commit()

    await login(client, "bob")
    resp = await client.get("/u/alice/private-bench/updates")
    assert resp.status_code == 404
    resp = await client.get(f"/u/alice/private-bench/updates/{update.id}")
    assert resp.status_code == 404


async def test_owner_feed_shows_public_indicator_only_on_public_entries(
    client, db,
):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
        is_public=True,
    )
    db.add(project)
    await db.flush()
    db.add_all(
        [
            ProjectUpdate(
                project_id=project.id, content="shared note", is_public=True
            ),
            ProjectUpdate(
                project_id=project.id, content="quiet note", is_public=False
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/bench/updates")
    body = resp.text
    # Only one Public-indicator tooltip should appear (for the public entry).
    assert body.count("visible to anyone who can see this project") == 1


async def test_updates_tab_loads_without_n_plus_one(client, db):
    """Guards the selectinload on Project.updates — if someone removes
    it, rendering the feed triggers raise_on_sql and this test fails."""
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    for i in range(3):
        db.add(ProjectUpdate(project_id=project.id, content=f"update {i}"))
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/bench/updates")
    assert resp.status_code == 200
    for i in range(3):
        assert f"update {i}" in resp.text

    # Sanity: the relationship is actually loaded (not a detached proxy)
    fresh = (
        await db.execute(
            select(Project).options(selectinload(Project.updates)).where(Project.id == project.id)
        )
    ).scalar_one()
    assert len(fresh.updates) == 3
