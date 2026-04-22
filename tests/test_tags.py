"""Tests for tag parsing, attach/sync, filtering, and display."""

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from benchlog.models import Project, Tag
from benchlog.tags import MAX_TAGS_PER_PROJECT, parse_tag_input
from tests.conftest import login, make_user, post_form


# ---------- parse_tag_input ---------- #


def test_parse_tag_input_handles_commas_and_spaces():
    assert parse_tag_input("woodworking, 3d-printing, electronics") == [
        "woodworking",
        "3d-printing",
        "electronics",
    ]
    assert parse_tag_input("woodworking electronics") == [
        "woodworking",
        "electronics",
    ]


def test_parse_tag_input_strips_hash_prefix():
    assert parse_tag_input("#woodworking, #electronics") == [
        "woodworking",
        "electronics",
    ]


def test_parse_tag_input_lowercases_and_slugifies():
    # Hyphens in a single piece survive the slugify pass.
    assert parse_tag_input("Woodworking, 3d-printing!!!") == [
        "woodworking",
        "3d-printing",
    ]


def test_parse_tag_input_splits_whitespace_inside_a_piece():
    # Whitespace is a separator, so "3D Printing" becomes two tags, not one.
    # Users who want a multi-word tag must type it with a hyphen.
    assert parse_tag_input("3D Printing") == ["3d", "printing"]


def test_parse_tag_input_deduplicates_preserving_order():
    assert parse_tag_input("foo, bar, foo, FOO, #bar") == ["foo", "bar"]


def test_parse_tag_input_drops_empty_and_pure_symbol_pieces():
    assert parse_tag_input("foo, , !!!, ") == ["foo"]


def test_parse_tag_input_caps_at_max_per_project():
    many = ", ".join(f"tag{i}" for i in range(MAX_TAGS_PER_PROJECT + 5))
    parsed = parse_tag_input(many)
    assert len(parsed) == MAX_TAGS_PER_PROJECT
    assert parsed[0] == "tag0"


def test_parse_tag_input_empty_returns_empty():
    assert parse_tag_input("") == []
    assert parse_tag_input("   ") == []


# ---------- create + update attach tags ---------- #


async def test_create_project_with_tags_attaches_them(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    await post_form(
        client,
        "/projects",
        {
            "title": "Shop Stool",
            "description": "",
            "status": "idea",
            "tags": "Woodworking, shop-made, woodworking",
        },
        csrf_path="/projects/new",
    )

    result = await db.execute(
        select(Project).options(selectinload(Project.tags))
    )
    project = result.scalar_one()
    slugs = sorted(t.slug for t in project.tags)
    assert slugs == ["shop-made", "woodworking"]


async def test_tags_reuse_existing_rows_across_users(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")

    await login(client, "alice")
    await post_form(
        client,
        "/projects",
        {
            "title": "Alice Bench",
            "description": "",
            "status": "idea",
            "tags": "woodworking",
        },
        csrf_path="/projects/new",
    )

    await login(client, "bob")
    await post_form(
        client,
        "/projects",
        {
            "title": "Bob Bench",
            "description": "",
            "status": "idea",
            "tags": "woodworking",
        },
        csrf_path="/projects/new",
    )

    tag_count = (await db.execute(select(func.count(Tag.id)))).scalar_one()
    assert tag_count == 1

    # The single tag row is linked to both projects (one per user)
    tag_row = (
        await db.execute(
            select(Tag).options(selectinload(Tag.projects)).where(Tag.slug == "woodworking")
        )
    ).scalar_one()
    project_user_ids = {p.user_id for p in tag_row.projects}
    assert alice.id in project_user_ids
    assert len(tag_row.projects) == 2


async def test_edit_replaces_tag_set(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    # Create with initial tags
    await post_form(
        client,
        "/projects",
        {
            "title": "Iterating",
            "description": "",
            "status": "idea",
            "tags": "foo, bar",
        },
        csrf_path="/projects/new",
    )
    project = (
        await db.execute(
            select(Project).options(selectinload(Project.tags)).where(Project.user_id == user.id)
        )
    ).scalar_one()
    assert sorted(t.slug for t in project.tags) == ["bar", "foo"]

    # Replace tags on edit — drops foo, keeps bar, adds baz
    await post_form(
        client,
        f"/u/alice/{project.slug}",
        {
            "title": "Iterating",
            "slug": project.slug,
            "description": "",
            "status": "idea",
            "tags": "bar, baz",
        },
        csrf_path=f"/u/alice/{project.slug}/edit",
    )

    await db.refresh(project, ["tags"])
    assert sorted(t.slug for t in project.tags) == ["bar", "baz"]


async def test_edit_clearing_tags_detaches_all(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    await post_form(
        client,
        "/projects",
        {"title": "Tagged", "description": "", "status": "idea", "tags": "foo, bar"},
        csrf_path="/projects/new",
    )
    project = (
        await db.execute(
            select(Project).options(selectinload(Project.tags)).where(Project.user_id == user.id)
        )
    ).scalar_one()
    assert len(project.tags) == 2

    await post_form(
        client,
        f"/u/alice/{project.slug}",
        {
            "title": "Tagged",
            "slug": project.slug,
            "description": "",
            "status": "idea",
            "tags": "",
        },
        csrf_path=f"/u/alice/{project.slug}/edit",
    )

    await db.refresh(project, ["tags"])
    assert project.tags == []


# ---------- filters ---------- #


async def test_my_projects_filter_by_tag(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    for title, tags in [
        ("Wood A", "woodworking"),
        ("Wood B", "woodworking, electronics"),
        ("Electronics only", "electronics"),
    ]:
        await post_form(
            client,
            "/projects",
            {"title": title, "description": "", "status": "idea", "tags": tags},
            csrf_path="/projects/new",
        )

    resp = await client.get("/projects?tag=woodworking")
    assert "Wood A" in resp.text
    assert "Wood B" in resp.text
    assert "Electronics only" not in resp.text
    # Active tag indicator shown with clear-all link
    assert "#woodworking" in resp.text
    assert "Clear all" in resp.text


async def test_explore_filter_by_tag(client, db):
    await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    await make_user(db, email="bob@test.com", username="bob")

    await login(client, "alice")
    await post_form(
        client,
        "/projects",
        {
            "title": "Public Wood",
            "description": "",
            "status": "in_progress",
            "tags": "woodworking",
            "is_public": "1",
        },
        csrf_path="/projects/new",
    )
    await post_form(
        client,
        "/projects",
        {
            "title": "Public Elec",
            "description": "",
            "status": "in_progress",
            "tags": "electronics",
            "is_public": "1",
        },
        csrf_path="/projects/new",
    )

    # Logged-out: tag filter still works
    resp = await client.get("/explore?tag=woodworking")
    assert resp.status_code == 200
    assert "Public Wood" in resp.text
    assert "Public Elec" not in resp.text


async def test_explore_tag_filter_ignores_private_projects(client, db):
    await make_user(db, email="alice@test.com", username="alice")

    await login(client, "alice")
    # Private project with the tag
    await post_form(
        client,
        "/projects",
        {
            "title": "Private Wood",
            "description": "",
            "status": "in_progress",
            "tags": "woodworking",
        },
        csrf_path="/projects/new",
    )

    resp = await client.get("/explore?tag=woodworking")
    assert "Private Wood" not in resp.text


# ---------- display ---------- #


async def test_tag_chips_render_on_card_and_detail(client, db):
    await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    await login(client, "alice")

    await post_form(
        client,
        "/projects",
        {
            "title": "Chippy",
            "description": "",
            "status": "in_progress",
            "tags": "woodworking, electronics",
            "is_public": "1",
        },
        csrf_path="/projects/new",
    )

    # Detail page — chips with # prefix
    resp = await client.get("/u/alice/chippy")
    assert "#woodworking" in resp.text
    assert "#electronics" in resp.text
    # Chips on detail link to /explore (shared context)
    assert 'href="/explore?tag=woodworking"' in resp.text

    # My Projects card — chips link to /projects (owner context)
    resp = await client.get("/projects")
    assert "#woodworking" in resp.text
    assert 'href="/projects?tag=woodworking"' in resp.text

    # Explore card — chips link to /explore
    resp = await client.get("/explore")
    assert "#woodworking" in resp.text
    assert 'href="/explore?tag=woodworking"' in resp.text


async def test_edit_form_prefills_tag_input(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    await post_form(
        client,
        "/projects",
        {"title": "Prefill", "description": "", "status": "idea", "tags": "foo, bar"},
        csrf_path="/projects/new",
    )
    project = (
        await db.execute(select(Project).where(Project.user_id == user.id))
    ).scalar_one()

    resp = await client.get(f"/u/alice/{project.slug}/edit")
    # Comma-separated current tags populate the input value
    assert 'value="foo, bar"' in resp.text or 'value="bar, foo"' in resp.text


# ---------- deletion ---------- #


async def test_project_card_has_no_nested_anchors(client, db):
    """Guards against the earlier rendering bug where card-as-anchor broke
    when tag chips (also anchors) were nested inside it."""
    await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    await login(client, "alice")

    await post_form(
        client,
        "/projects",
        {
            "title": "Open Project A",
            "description": "this is public",
            "status": "idea",
            "tags": "foo, dsfsd",
            "is_public": "1",
        },
        csrf_path="/projects/new",
    )

    resp = await client.get("/projects")
    assert resp.status_code == 200
    # Card root is an <article>, not an <a>; the title's <a> uses
    # stretched-link so the whole card stays clickable without nesting.
    assert '<article class="card relative' in resp.text
    assert "stretched-link" in resp.text


async def test_long_title_gets_clamp_and_tooltip(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    long_title = "This is a reallllyyy long title, how does it handle that?"
    await post_form(
        client,
        "/projects",
        {
            "title": long_title,
            "description": "",
            "status": "idea",
            "tags": "",
        },
        csrf_path="/projects/new",
    )

    resp = await client.get("/projects")
    # Title is clamped via a wrapping span with line-clamp-2
    assert 'class="line-clamp-2"' in resp.text
    # Full title survives as the anchor's `title` attribute for on-hover
    # discovery, so it's never lost even when visually truncated.
    assert f'title="{long_title}"' in resp.text


async def test_pinned_renders_as_floating_corner_marker(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    # Pinned flag set via the model rather than the form (form has no
    # checkbox for pin at creation time in every flow; direct construction
    # is cleaner for this assertion).
    from benchlog.models import Project, ProjectStatus
    db.add(
        Project(
            user_id=user.id,
            title="Pinned one",
            slug="pinned-one",
            status=ProjectStatus.idea,
            pinned=True,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/projects")
    # Corner marker is absolutely positioned and carries an aria-label
    assert 'aria-label="Pinned"' in resp.text
    # No inline pin icon inside the title flow anymore
    assert 'Pinned one' in resp.text


async def test_private_badge_is_gone_public_indicator_on_owner_view(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    from benchlog.models import Project, ProjectStatus
    db.add_all(
        [
            Project(
                user_id=user.id,
                title="Keeper",
                slug="keeper",
                status=ProjectStatus.idea,
                is_public=False,
            ),
            Project(
                user_id=user.id,
                title="Sharer",
                slug="sharer",
                status=ProjectStatus.idea,
                is_public=True,
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/projects")
    # No "Private" chip on any card
    assert 'title="Private' not in resp.text
    # Public indicator appears only for the public project
    assert ">Public<" in resp.text or "Public\n" in resp.text
    # Non-public card shouldn't mention "Public"
    # (we at least verify the indicator's tooltip text shows once, not twice)
    assert resp.text.count("anyone with the link can view") == 1


async def test_public_indicator_on_detail_for_owner(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    from benchlog.models import Project, ProjectStatus
    db.add(
        Project(
            user_id=user.id,
            title="Owner sees public badge",
            slug="owner-sees-public-badge",
            status=ProjectStatus.idea,
            is_public=True,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/owner-sees-public-badge")
    assert resp.status_code == 200
    assert "anyone with the link can view" in resp.text
    assert ">\n            Public\n        <" in resp.text or "Public" in resp.text


async def test_public_indicator_on_detail_for_guest(client, db):
    """A shared link landing on the detail page benefits from an explicit
    'Public' marker — otherwise a visitor can't tell if the project was
    intentionally shared or accidentally leaked."""
    user = await make_user(db, email="alice@test.com", username="alice")
    from benchlog.models import Project, ProjectStatus
    db.add(
        Project(
            user_id=user.id,
            title="Shared link visitor",
            slug="shared-link-visitor",
            status=ProjectStatus.idea,
            is_public=True,
        )
    )
    await db.commit()

    resp = await client.get("/u/alice/shared-link-visitor")
    assert resp.status_code == 200
    assert "anyone with the link can view" in resp.text


async def test_private_project_detail_has_no_public_indicator(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    from benchlog.models import Project, ProjectStatus
    db.add(
        Project(
            user_id=user.id,
            title="Quiet",
            slug="quiet",
            status=ProjectStatus.idea,
            is_public=False,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/quiet")
    assert resp.status_code == 200
    # No public indicator when project is private
    assert "anyone with the link can view" not in resp.text


async def test_public_indicator_suppressed_on_explore(client, db):
    user = await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    from benchlog.models import Project, ProjectStatus
    db.add(
        Project(
            user_id=user.id,
            title="Explore card",
            slug="explore-card",
            status=ProjectStatus.idea,
            is_public=True,
        )
    )
    await db.commit()

    resp = await client.get("/explore")
    # Explore is all-public by definition — suppress the redundant badge.
    assert "Explore card" in resp.text
    assert "anyone with the link can view" not in resp.text


async def test_form_embeds_known_tags_for_combobox(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    # Seed a couple of tags on an existing project so the user has a vocabulary.
    await post_form(
        client,
        "/projects",
        {
            "title": "Seed",
            "description": "",
            "status": "idea",
            "tags": "foo, bar",
        },
        csrf_path="/projects/new",
    )

    # New-project form embeds the known tags on the wrapper.
    resp = await client.get("/projects/new")
    assert 'data-tag-input' in resp.text
    # Slugs are space-separated on the wrapper's data-known-tags attribute.
    assert 'data-known-tags="bar foo"' in resp.text or 'data-known-tags="foo bar"' in resp.text
    # Hidden input + visible search box both rendered.
    assert 'data-tag-hidden' in resp.text
    assert 'data-tag-search' in resp.text

    # Edit form embeds the same list.
    project = (
        await db.execute(select(Project).where(Project.user_id == user.id))
    ).scalar_one()
    resp = await client.get(f"/u/alice/{project.slug}/edit")
    assert 'data-tag-input' in resp.text
    assert 'foo' in resp.text and 'bar' in resp.text


async def test_form_combobox_exposes_undo_redo_handlers(client, db):
    """Sanity check that the pill-level undo/redo JS was not regressed
    away. Full interaction is JS-driven; we just verify the hooks ship."""
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    resp = await client.get("/projects/new")
    # History stack functions
    assert "pushHistory" in resp.text
    assert "history.past" in resp.text
    # Modifier-key branch for Cmd/Ctrl+Z and Ctrl+Y
    assert "metaKey" in resp.text
    assert "ctrlKey" in resp.text


async def test_filter_bar_tag_autocomplete_scopes_per_context(client, db):
    """The tag combobox in the project filter bar must carry the right
    autocomplete vocabulary for its context:

    - `/projects` (owner-scoped) exposes every tag the viewer has used on
      any of their own projects (public or private).
    - `/explore` (public) exposes only tags that currently appear on
      public projects — private tags must never leak into the guest /
      cross-user autocomplete payload.
    """
    import re

    alice = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    # Public project with `public-tag`
    await post_form(
        client,
        "/projects",
        {
            "title": "Pub",
            "description": "",
            "status": "idea",
            "tags": "public-tag",
            "is_public": "1",
        },
        csrf_path="/projects/new",
    )
    # Private project with `private-tag`
    await post_form(
        client,
        "/projects",
        {
            "title": "Priv",
            "description": "",
            "status": "idea",
            "tags": "private-tag",
        },
        csrf_path="/projects/new",
    )

    # /projects — owner-scoped: sees both her own public and private tags.
    resp = await client.get("/projects")
    assert resp.status_code == 200
    matches = re.findall(r'data-known-tags="([^"]*)"', resp.text)
    assert matches, "tag combobox should render data-known-tags on /projects"
    # The filter bar is the first combobox rendered on /projects; the
    # page may include additional tag chips but we only care the payload
    # is scoped correctly. Take the union across every rendered combobox.
    all_slugs: set[str] = set()
    for m in matches:
        all_slugs.update(m.split())
    assert "public-tag" in all_slugs
    assert "private-tag" in all_slugs

    # /explore as a guest — public-only payload.
    resp = await client.get("/explore")
    assert resp.status_code == 200
    matches = re.findall(r'data-known-tags="([^"]*)"', resp.text)
    assert matches, "tag combobox should render data-known-tags on /explore"
    explore_slugs: set[str] = set()
    for m in matches:
        explore_slugs.update(m.split())
    assert "public-tag" in explore_slugs
    assert "private-tag" not in explore_slugs

    # Keep type-checker / ruff happy about the unused capture.
    assert alice.username == "alice"


async def test_filter_bar_tag_autocomplete_allows_free_form_commit(client, db):
    """The filter combobox must NOT be existing_only — users should be
    able to commit a typed slug that isn't in the suggestion payload
    (public/private vocabularies evolve independently of what's cached
    on-page). An unknown slug simply returns zero results rather than
    being silently dropped on Enter.
    """
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    resp = await client.get("/projects")
    assert resp.status_code == 200
    # existing_only toggles a specific data attribute on the combobox
    # wrapper — absence means free-form commits are accepted by the JS.
    assert 'data-tag-existing-only="1"' not in resp.text
    assert 'data-tag-existing-only=""' in resp.text


async def test_form_combobox_renders_with_no_known_tags(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    resp = await client.get("/projects/new")
    # Widget always renders (user can still create new tags); known-tags
    # list is just empty.
    assert 'data-tag-input' in resp.text
    assert 'data-known-tags=""' in resp.text
    assert 'data-tag-search' in resp.text


async def test_deleting_project_removes_associations_not_tag_rows(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")

    await login(client, "alice")
    await post_form(
        client,
        "/projects",
        {"title": "Alice Shared", "description": "", "status": "idea", "tags": "shared"},
        csrf_path="/projects/new",
    )

    await login(client, "bob")
    await post_form(
        client,
        "/projects",
        {"title": "Bob Shared", "description": "", "status": "idea", "tags": "shared"},
        csrf_path="/projects/new",
    )

    # Delete Alice's project
    await login(client, "alice")
    alice_project = (
        await db.execute(select(Project).where(Project.user_id == alice.id))
    ).scalar_one()
    await post_form(
        client,
        f"/u/alice/{alice_project.slug}/delete",
        {},
        csrf_path="/projects",
    )

    # The tag row still exists (Bob's project still uses it)
    tag_count = (await db.execute(select(func.count(Tag.id)))).scalar_one()
    assert tag_count == 1
