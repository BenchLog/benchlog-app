"""Tests for the project journal — CRUD, ordering, visibility, pinning,
per-entry slug behaviour, markdown render, and rename-tracking for
`journal/<slug>` links."""

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from benchlog.models import JournalEntry, Project, ProjectStatus
from tests.conftest import login, make_user, post_form


# ---------- create ---------- #


async def test_owner_creates_entry_and_redirects_to_project(client, db):
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
        "/u/alice/bench/journal",
        {"title": "Day 1", "content": "Glued the tenons."},
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 302
    assert "/u/alice/bench/journal#entry-" in resp.headers["location"]

    entry = (await db.execute(select(JournalEntry))).scalar_one()
    assert entry.title == "Day 1"
    assert entry.content == "Glued the tenons."
    # Titled entry gets a per-project slug derived from the title.
    assert entry.slug == "day-1"


async def test_untitled_entry_has_no_slug(client, db):
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
        "/u/alice/bench/journal",
        {"title": "", "content": "Untitled but substantive."},
        csrf_path="/u/alice/bench",
    )
    entry = (await db.execute(select(JournalEntry))).scalar_one()
    assert entry.title is None
    assert entry.slug is None


async def test_create_entry_requires_content(client, db):
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
        "/u/alice/bench/journal",
        {"title": "Stub", "content": "   "},
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 400
    assert "Content is required." in resp.text

    remaining = (await db.execute(select(JournalEntry))).scalars().all()
    assert remaining == []


async def test_slug_is_deduped_within_project(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    db.add(JournalEntry(project_id=project.id, title="Day 1", slug="day-1", content="first"))
    await db.commit()

    await login(client, "alice")
    await post_form(
        client,
        "/u/alice/bench/journal",
        {"title": "Day 1", "content": "second"},
        csrf_path="/u/alice/bench",
    )
    entries = (await db.execute(select(JournalEntry))).scalars().all()
    slugs = sorted(e.slug for e in entries)
    assert slugs == ["day-1", "day-1-2"]


async def test_slug_reusable_across_projects(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    p1 = Project(
        user_id=user.id, title="A", slug="a", status=ProjectStatus.idea
    )
    p2 = Project(
        user_id=user.id, title="B", slug="b", status=ProjectStatus.idea
    )
    db.add_all([p1, p2])
    await db.commit()

    await login(client, "alice")
    await post_form(
        client,
        "/u/alice/a/journal",
        {"title": "Day 1", "content": "for a"},
        csrf_path="/u/alice/a",
    )
    await post_form(
        client,
        "/u/alice/b/journal",
        {"title": "Day 1", "content": "for b"},
        csrf_path="/u/alice/b",
    )
    entries = (await db.execute(select(JournalEntry))).scalars().all()
    slugs = sorted(e.slug for e in entries)
    # Same slug allowed — uniqueness is per-project, not per-user.
    assert slugs == ["day-1", "day-1"]


# ---------- feed ordering ---------- #


async def test_journal_feed_is_newest_first(client, db):
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
    db.add_all(
        [
            JournalEntry(
                project_id=project.id, content="marker-alpha", created_at=base
            ),
            JournalEntry(
                project_id=project.id,
                content="marker-bravo",
                created_at=base + timedelta(hours=1),
            ),
            JournalEntry(
                project_id=project.id,
                content="marker-charlie",
                created_at=base + timedelta(hours=2),
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/bench/journal")
    body = resp.text
    assert (
        body.index("marker-charlie")
        < body.index("marker-bravo")
        < body.index("marker-alpha")
    )


async def test_pinned_entries_sort_first(client, db):
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
    # Newest unpinned should still land below the oldest pinned.
    db.add_all(
        [
            JournalEntry(
                project_id=project.id,
                content="marker-old-pinned",
                created_at=base,
                is_pinned=True,
            ),
            JournalEntry(
                project_id=project.id,
                content="marker-new-unpinned",
                created_at=base + timedelta(hours=5),
            ),
            JournalEntry(
                project_id=project.id,
                content="marker-mid-pinned",
                created_at=base + timedelta(hours=2),
                is_pinned=True,
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/bench/journal")
    body = resp.text
    # Pinned entries come first, newest-pinned beats older-pinned.
    assert (
        body.index("marker-mid-pinned")
        < body.index("marker-old-pinned")
        < body.index("marker-new-unpinned")
    )


# ---------- pin / unpin ---------- #


async def test_owner_can_pin_and_unpin_entry(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    entry = JournalEntry(
        project_id=project.id, title="Keep", slug="keep", content="body"
    )
    db.add(entry)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/bench/journal/{entry.slug}/pin",
        {},
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/bench/journal"
    await db.refresh(entry)
    assert entry.is_pinned is True

    resp = await post_form(
        client,
        f"/u/alice/bench/journal/{entry.slug}/unpin",
        {},
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 302
    await db.refresh(entry)
    assert entry.is_pinned is False


async def test_non_owner_cannot_pin(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    project = Project(
        user_id=alice.id,
        title="Public",
        slug="public",
        status=ProjectStatus.in_progress,
        is_public=True,
    )
    db.add(project)
    await db.flush()
    entry = JournalEntry(
        project_id=project.id,
        title="Visible",
        slug="visible",
        content="body",
        is_public=True,
    )
    db.add(entry)
    await db.commit()

    await login(client, "bob")
    resp = await post_form(
        client,
        f"/u/alice/public/journal/{entry.slug}/pin",
        {},
        csrf_path="/projects",
    )
    assert resp.status_code == 404
    await db.refresh(entry)
    assert entry.is_pinned is False


async def test_pin_unknown_entry_is_404(client, db):
    import uuid

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
        f"/u/alice/bench/journal/{uuid.uuid4()}/pin",
        {},
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 404


# ---------- sticky slug ---------- #


async def test_title_edit_does_not_change_slug(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    entry = JournalEntry(
        project_id=project.id, title="First", slug="first", content="body"
    )
    db.add(entry)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/bench/journal/{entry.slug}",
        {"title": "Totally different title", "slug": "first", "content": "body"},
        csrf_path=f"/u/alice/bench/journal/{entry.slug}/edit",
    )
    assert resp.status_code == 302
    await db.refresh(entry)
    assert entry.title == "Totally different title"
    # Sticky: slug stayed as the original.
    assert entry.slug == "first"


async def test_slug_edit_changes_slug(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    entry = JournalEntry(
        project_id=project.id, title="First", slug="first", content="body"
    )
    db.add(entry)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/bench/journal/{entry.slug}",
        {"title": "First", "slug": "moved", "content": "body"},
        csrf_path=f"/u/alice/bench/journal/{entry.slug}/edit",
    )
    assert resp.status_code == 302
    await db.refresh(entry)
    assert entry.slug == "moved"


async def test_clearing_title_nulls_slug(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    entry = JournalEntry(
        project_id=project.id, title="First", slug="first", content="body"
    )
    db.add(entry)
    await db.commit()

    await login(client, "alice")
    await post_form(
        client,
        f"/u/alice/bench/journal/{entry.slug}",
        {"title": "", "slug": "first", "content": "body"},
        csrf_path=f"/u/alice/bench/journal/{entry.slug}/edit",
    )
    await db.refresh(entry)
    assert entry.title is None
    assert entry.slug is None


async def test_adding_title_generates_slug(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    entry = JournalEntry(project_id=project.id, title=None, content="body")
    db.add(entry)
    await db.commit()

    await login(client, "alice")
    await post_form(
        client,
        f"/u/alice/bench/journal/{entry.id}",
        {"title": "Now titled", "content": "body"},
        csrf_path=f"/u/alice/bench/journal/{entry.id}/edit",
    )
    await db.refresh(entry)
    assert entry.title == "Now titled"
    assert entry.slug == "now-titled"


# ---------- detail permalink ---------- #


async def test_titled_entry_has_detail_url(client, db):
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
    entry = JournalEntry(
        project_id=project.id,
        title="Finished",
        slug="finished",
        content="Glued tenons.",
        is_public=True,
    )
    db.add(entry)
    await db.commit()

    resp = await client.get("/u/alice/public-bench/journal/finished")
    assert resp.status_code == 200
    assert "Finished" in resp.text
    assert "Glued tenons." in resp.text


async def test_untitled_entry_has_no_detail_url(client, db):
    """Title-less entries only live on the feed — their slug is NULL and
    the slug route path can't resolve them."""
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
    entry = JournalEntry(
        project_id=project.id, title=None, content="just a note", is_public=True
    )
    db.add(entry)
    await db.commit()

    # The slug would be NULL, and the detail route takes a slug string —
    # anything we probe lands on 404 (or on the `/new` form when the
    # probe string is literally `new`, but that would 401/403 for a
    # guest). Probe with an arbitrary slug-shaped value.
    resp = await client.get("/u/alice/bench/journal/nonexistent")
    assert resp.status_code == 404


# ---------- edit ---------- #


async def test_owner_can_edit_entry(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    entry = JournalEntry(
        project_id=project.id, title="Initial", slug="initial", content="draft"
    )
    db.add(entry)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/bench/journal/{entry.slug}",
        {"title": "Initial", "slug": "initial", "content": "final"},
        csrf_path=f"/u/alice/bench/journal/{entry.slug}/edit",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/bench/journal/initial"

    await db.refresh(entry)
    assert entry.content == "final"


# ---------- delete ---------- #


async def test_owner_can_delete_entry(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    entry = JournalEntry(
        project_id=project.id, title="Gone", slug="gone", content="soon gone"
    )
    db.add(entry)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/bench/journal/{entry.slug}/delete",
        {},
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/bench/journal"

    remaining = (await db.execute(select(JournalEntry))).scalars().all()
    assert remaining == []


# ---------- visibility & access control ---------- #


async def test_non_owner_cannot_edit_or_delete_entry(client, db):
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
    entry = JournalEntry(
        project_id=project.id,
        title="Visible",
        slug="visible",
        content="public entry",
        is_public=True,
    )
    db.add(entry)
    await db.commit()

    await login(client, "bob")

    resp = await client.get(f"/u/alice/alice-public/journal/{entry.slug}/edit")
    assert resp.status_code == 404

    resp = await post_form(
        client,
        f"/u/alice/alice-public/journal/{entry.slug}",
        {"title": "pwned", "slug": "visible", "content": "gotcha"},
        csrf_path="/projects",
    )
    assert resp.status_code == 404

    resp = await post_form(
        client,
        f"/u/alice/alice-public/journal/{entry.slug}/delete",
        {},
        csrf_path="/projects",
    )
    assert resp.status_code == 404

    await db.refresh(entry)
    assert entry.content == "public entry"


async def test_guest_cannot_view_entry_on_private_project(client, db):
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
    entry = JournalEntry(
        project_id=project.id, title="shh", slug="shh", content="shh"
    )
    db.add(entry)
    await db.commit()

    resp = await client.get("/u/alice/private-bench/journal/shh")
    assert resp.status_code == 404


async def test_guest_post_to_journal_redirects_to_login(client, db):
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
        "/u/alice/bench/journal",
        data={"title": "x", "content": "y"},
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


async def test_guest_new_entry_form_redirects_to_login(client, db):
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

    resp = await client.get("/u/alice/bench/journal/new")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


# ---------- rendering ---------- #


async def test_entry_markdown_renders_as_html_on_feed(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    entry = JournalEntry(
        project_id=project.id,
        content="## Heading\n\n- [x] done\n- [ ] todo",
    )
    db.add(entry)
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/bench/journal")
    assert "<h2>Heading</h2>" in resp.text
    assert "task-list-item" in resp.text


async def test_entry_escapes_raw_html_in_input(client, db):
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
        "/u/alice/bench/journal",
        {"title": "", "content": "<script>alert('x')</script>\n\nsafe body"},
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/journal")
    assert "<script>alert('x')</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text
    assert "safe body" in resp.text


# ---------- cascade ---------- #


async def test_deleting_project_cascades_to_journal_entries(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    db.add(JournalEntry(project_id=project.id, content="a"))
    db.add(JournalEntry(project_id=project.id, content="b"))
    await db.commit()

    await login(client, "alice")
    await post_form(
        client,
        "/u/alice/bench/delete",
        {},
        csrf_path="/projects",
    )

    remaining = (await db.execute(select(JournalEntry))).scalars().all()
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
    entry = JournalEntry(
        project_id=project.id, title="T", slug="t", content="body"
    )
    db.add(entry)
    await db.commit()

    await login(client, "alice")

    # Overview — Overview tab is current, Journal tab is not.
    resp = await client.get("/u/alice/bench")
    assert resp.status_code == 200
    body = resp.text
    assert 'href="/u/alice/bench"' in body
    assert 'href="/u/alice/bench/journal"' in body
    overview_anchor = body[
        body.index('href="/u/alice/bench"') : body.index("Overview") + len("Overview")
    ]
    journal_anchor = body[
        body.index('href="/u/alice/bench/journal"') : body.index("Journal") + len("Journal")
    ]
    assert 'aria-current="page"' in overview_anchor
    assert 'aria-current="page"' not in journal_anchor

    # /journal — Journal tab active.
    resp = await client.get("/u/alice/bench/journal")
    assert resp.status_code == 200
    body = resp.text
    start = body.index('href="/u/alice/bench/journal"')
    assert 'aria-current="page"' in body[start : start + 300]

    # Single entry page — still under Journal tab.
    resp = await client.get(f"/u/alice/bench/journal/{entry.slug}")
    assert resp.status_code == 200
    body = resp.text
    start = body.index('href="/u/alice/bench/journal"')
    assert 'aria-current="page"' in body[start : start + 300]


async def test_journal_tab_shows_full_feed(client, db):
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
            JournalEntry(
                project_id=project.id,
                content=f"entry-{i}",
                created_at=base + timedelta(hours=i),
            )
        )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/bench/journal")
    assert resp.status_code == 200
    for i in range(5):
        assert f"entry-{i}" in resp.text


async def test_overview_shows_no_journal_feed(client, db):
    """Overview is description-only; the feed lives on the Journal tab."""
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
    db.add(JournalEntry(project_id=project.id, content="entry-body"))
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/bench")
    assert resp.status_code == 200
    assert "A shop stool." in resp.text
    assert "entry-body" not in resp.text


async def test_guest_can_load_journal_tab_on_public_project(client, db):
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
    db.add(
        JournalEntry(
            project_id=project.id, content="open entry", is_public=True
        )
    )
    await db.commit()

    resp = await client.get("/u/alice/bench/journal")
    assert resp.status_code == 200
    assert "open entry" in resp.text


async def test_guest_journal_tab_404s_on_private_project(client, db):
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

    resp = await client.get("/u/alice/bench/journal")
    assert resp.status_code == 404


# ---------- per-entry visibility ---------- #


async def test_new_entry_defaults_to_private(client, db):
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
        "/u/alice/bench/journal",
        {"title": "", "content": "quiet note"},
        csrf_path="/u/alice/bench",
    )
    entry = (await db.execute(select(JournalEntry))).scalar_one()
    assert entry.is_public is False


async def test_owner_can_flip_entry_public_via_form(client, db):
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
        "/u/alice/bench/journal",
        {"title": "", "content": "visible from the start", "is_public": "1"},
        csrf_path="/u/alice/bench",
    )
    entry = (await db.execute(select(JournalEntry))).scalar_one()
    assert entry.is_public is True

    await post_form(
        client,
        f"/u/alice/bench/journal/{entry.id}",
        {"title": "", "content": "visible from the start"},
        csrf_path=f"/u/alice/bench/journal/{entry.id}/edit",
    )
    await db.refresh(entry)
    assert entry.is_public is False


async def test_feed_filters_private_entries_for_non_owners(client, db):
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
            JournalEntry(
                project_id=project.id, content="alice-public", is_public=True
            ),
            JournalEntry(
                project_id=project.id, content="alice-private", is_public=False
            ),
        ]
    )
    await db.commit()

    resp = await client.get("/u/alice/public-bench/journal")
    assert "alice-public" in resp.text
    assert "alice-private" not in resp.text

    await login(client, "bob")
    resp = await client.get("/u/alice/public-bench/journal")
    assert "alice-public" in resp.text
    assert "alice-private" not in resp.text


async def test_feed_shows_private_entries_to_owner(client, db):
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
            JournalEntry(
                project_id=project.id, content="alice-public", is_public=True
            ),
            JournalEntry(
                project_id=project.id, content="alice-private", is_public=False
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/public-bench/journal")
    assert "alice-public" in resp.text
    assert "alice-private" in resp.text


async def test_private_entry_permalink_404s_for_non_owner(client, db):
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
    entry = JournalEntry(
        project_id=project.id,
        title="Draft",
        slug="draft",
        content="draft thought",
        is_public=False,
    )
    db.add(entry)
    await db.commit()

    resp = await client.get("/u/alice/public-bench/journal/draft")
    assert resp.status_code == 404

    await login(client, "bob")
    resp = await client.get("/u/alice/public-bench/journal/draft")
    assert resp.status_code == 404


async def test_private_project_hides_even_public_entries_from_non_owners(
    client, db,
):
    """Project visibility dominates: a public entry on a private project
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
    entry = JournalEntry(
        project_id=project.id,
        title="Would",
        slug="would",
        content="would be public",
        is_public=True,
    )
    db.add(entry)
    await db.commit()

    await login(client, "bob")
    resp = await client.get("/u/alice/private-bench/journal")
    assert resp.status_code == 404
    resp = await client.get("/u/alice/private-bench/journal/would")
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
            JournalEntry(
                project_id=project.id, content="shared note", is_public=True
            ),
            JournalEntry(
                project_id=project.id, content="quiet note", is_public=False
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/bench/journal")
    body = resp.text
    assert body.count("visible to anyone who can see this project") == 1


async def test_journal_tab_loads_without_n_plus_one(client, db):
    """Guards the selectinload on Project.journal_entries — if someone
    removes it, rendering the feed triggers raise_on_sql and this test
    fails."""
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
        db.add(JournalEntry(project_id=project.id, content=f"entry {i}"))
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/bench/journal")
    assert resp.status_code == 200
    for i in range(3):
        assert f"entry {i}" in resp.text

    fresh = (
        await db.execute(
            select(Project)
            .options(selectinload(Project.journal_entries))
            .where(Project.id == project.id)
        )
    ).scalar_one()
    assert len(fresh.journal_entries) == 3


# ---------- autocomplete index ---------- #


async def test_project_entry_index_excludes_untitled(client, db):
    """Titled entries feed the `journal/…` autocomplete; untitled ones
    don't have a slug to link to so they stay out."""
    import re

    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    db.add_all(
        [
            JournalEntry(
                project_id=project.id, title="Day 1", slug="day-1", content="first"
            ),
            JournalEntry(
                project_id=project.id, title=None, content="untitled note"
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/bench/journal/new")
    assert resp.status_code == 200
    # Server renders the entry index JSON on the mount's
    # `data-toastui-entry-index` attr — a single entry (untitled
    # excluded).
    m = re.search(r"data-toastui-entry-index='([^']*)'", resp.text)
    assert m is not None
    import json as _json
    index = _json.loads(m.group(1))
    assert [e["slug"] for e in index] == ["day-1"]


# ---------- rename-tracking ---------- #


async def test_slug_rename_rewrites_project_description(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        description="See [the kickoff](journal/day-1) for context.",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    entry = JournalEntry(
        project_id=project.id,
        title="Day 1",
        slug="day-1",
        content="kickoff",
    )
    db.add(entry)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/bench/journal/{entry.slug}",
        {"title": "Day 1", "slug": "kickoff", "content": "kickoff"},
        csrf_path=f"/u/alice/bench/journal/{entry.slug}/edit",
    )
    assert resp.status_code == 302

    # Refresh forces a re-read after the route's separate session
    # committed — the test session's identity map would otherwise hand
    # back its stale copy with the pre-rewrite description.
    await db.refresh(project)
    assert project.description == "See [the kickoff](journal/kickoff) for context."


async def test_slug_rename_rewrites_sibling_entry_body(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    a = JournalEntry(
        project_id=project.id,
        title="Day 1",
        slug="day-1",
        content="target",
    )
    b = JournalEntry(
        project_id=project.id,
        title="Day 2",
        slug="day-2",
        content="Continuing from [day one](journal/day-1).",
    )
    db.add_all([a, b])
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/bench/journal/{a.slug}",
        {"title": "Day 1", "slug": "kickoff", "content": "target"},
        csrf_path=f"/u/alice/bench/journal/{a.slug}/edit",
    )
    assert resp.status_code == 302

    await db.refresh(b)
    assert b.content == "Continuing from [day one](journal/kickoff)."


async def test_title_rename_rewrites_matching_labels(client, db):
    # Links whose visible text matches the old title get rewritten to
    # the new title — same pattern files use. Custom labels ("the kickoff")
    # are preserved because only exact matches trigger the swap.
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        description=(
            "[Day 1](journal/day-1) was the start. "
            "See [the kickoff](journal/day-1) for more."
        ),
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    entry = JournalEntry(
        project_id=project.id,
        title="Day 1",
        slug="day-1",
        content="kickoff",
    )
    db.add(entry)
    await db.commit()

    await login(client, "alice")
    # Title-only edit — slug is sticky (not submitted, left blank).
    resp = await post_form(
        client,
        f"/u/alice/bench/journal/{entry.slug}",
        {"title": "Kickoff Day", "slug": "day-1", "content": "kickoff"},
        csrf_path=f"/u/alice/bench/journal/{entry.slug}/edit",
    )
    assert resp.status_code == 302

    await db.refresh(project)
    # Label matching old title is swapped; user-custom label left alone;
    # URL unchanged because slug stayed put.
    assert project.description == (
        "[Kickoff Day](journal/day-1) was the start. "
        "See [the kickoff](journal/day-1) for more."
    )
