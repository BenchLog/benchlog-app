"""Tests for project links — CRUD, URL normalization, visibility, ordering."""

from sqlalchemy import select

from benchlog.links import normalize_url, parse_link_type
from benchlog.models import LinkType, Project, ProjectLink, ProjectStatus
from tests.conftest import login, make_user, post_form


# ---------- URL normalization ---------- #


def test_normalize_url_adds_https_when_missing_scheme():
    assert normalize_url("github.com/alice/proj") == "https://github.com/alice/proj"


def test_normalize_url_preserves_http_and_https():
    assert normalize_url("http://example.com") == "http://example.com"
    assert normalize_url("https://example.com/path") == "https://example.com/path"


def test_normalize_url_accepts_non_web_schemes():
    # Non-web schemes pass through untouched — users may legitimately link
    # to mail addresses, SSH servers, or custom protocols.
    assert normalize_url("mailto:alice@example.com") == "mailto:alice@example.com"
    assert normalize_url("ftp://example.com/file") == "ftp://example.com/file"
    assert normalize_url("ssh://server.example") == "ssh://server.example"
    assert normalize_url("file:///etc/passwd") == "file:///etc/passwd"


def test_normalize_url_blocks_xss_schemes():
    # javascript: / data: / vbscript: would execute when rendered inside
    # <a href="...">, so they're rejected even though the input is
    # otherwise "valid". Case-insensitive.
    assert normalize_url("javascript:alert(1)") == ""
    assert normalize_url("JavaScript:alert(1)") == ""
    assert normalize_url("data:text/html,<script>alert(1)</script>") == ""
    assert normalize_url("vbscript:msgbox(1)") == ""


def test_normalize_url_rejects_empty_and_whitespace():
    assert normalize_url("") == ""
    assert normalize_url("   ") == ""
    assert normalize_url("https://") == ""  # no host


def test_parse_link_type_defaults_to_other_on_unknown():
    assert parse_link_type("github") == LinkType.github
    assert parse_link_type("") == LinkType.other
    assert parse_link_type(None) == LinkType.other
    assert parse_link_type("not-a-type") == LinkType.other


# ---------- create ---------- #


async def test_owner_adds_link_and_redirects_to_links_tab(client, db):
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
        "/u/alice/bench/links",
        {
            "title": "Source",
            "url": "https://github.com/alice/bench",
            "link_type": "github",
        },
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/bench/links"

    link = (await db.execute(select(ProjectLink))).scalar_one()
    assert link.title == "Source"
    assert link.url == "https://github.com/alice/bench"
    assert link.link_type == LinkType.github


async def test_create_link_auto_https_when_scheme_missing(client, db):
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
        "/u/alice/bench/links",
        {"title": "Site", "url": "example.com", "link_type": "website"},
        csrf_path="/u/alice/bench",
    )
    link = (await db.execute(select(ProjectLink))).scalar_one()
    assert link.url == "https://example.com"


async def test_create_link_rejects_missing_title(client, db):
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
        "/u/alice/bench/links",
        {"title": "   ", "url": "https://example.com", "link_type": "other"},
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 400
    assert "Title is required." in resp.text

    remaining = (await db.execute(select(ProjectLink))).scalars().all()
    assert remaining == []


async def test_create_link_rejects_xss_scheme(client, db):
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
        "/u/alice/bench/links",
        {"title": "Bad", "url": "javascript:alert(1)", "link_type": "other"},
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 400
    assert "valid URL" in resp.text

    remaining = (await db.execute(select(ProjectLink))).scalars().all()
    assert remaining == []


async def test_create_link_accepts_mailto(client, db):
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
        "/u/alice/bench/links",
        {
            "title": "Email me",
            "url": "mailto:alice@example.com",
            "link_type": "other",
        },
        csrf_path="/u/alice/bench",
    )
    link = (await db.execute(select(ProjectLink))).scalar_one()
    assert link.url == "mailto:alice@example.com"


async def test_create_link_unknown_type_defaults_to_other(client, db):
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
        "/u/alice/bench/links",
        {
            "title": "Free form",
            "url": "https://example.com",
            "link_type": "not-a-real-type",
        },
        csrf_path="/u/alice/bench",
    )
    link = (await db.execute(select(ProjectLink))).scalar_one()
    assert link.link_type == LinkType.other


# ---------- edit + delete ---------- #


async def test_owner_can_edit_link(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.flush()
    link = ProjectLink(
        project_id=project.id,
        title="Old",
        url="https://old.example.com",
        link_type=LinkType.other,
    )
    db.add(link)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/bench/links/{link.id}",
        {
            "title": "New",
            "url": "https://new.example.com",
            "link_type": "documentation",
        },
        csrf_path=f"/u/alice/bench/links/{link.id}/edit",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/bench/links"

    await db.refresh(link)
    assert link.title == "New"
    assert link.url == "https://new.example.com"
    assert link.link_type == LinkType.documentation


async def test_owner_can_delete_link(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.flush()
    link = ProjectLink(
        project_id=project.id,
        title="Disposable",
        url="https://example.com",
        link_type=LinkType.other,
    )
    db.add(link)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/bench/links/{link.id}/delete",
        {},
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/bench/links"

    remaining = (await db.execute(select(ProjectLink))).scalars().all()
    assert remaining == []


# ---------- visibility (inherits project) ---------- #


async def test_non_owner_cannot_edit_or_delete_links(client, db):
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
    link = ProjectLink(
        project_id=project.id,
        title="Source",
        url="https://example.com",
        link_type=LinkType.github,
    )
    db.add(link)
    await db.commit()

    await login(client, "bob")

    resp = await client.get(f"/u/alice/alice-public/links/{link.id}/edit")
    assert resp.status_code == 404

    resp = await post_form(
        client,
        f"/u/alice/alice-public/links/{link.id}",
        {"title": "Hijacked", "url": "https://evil.example", "link_type": "other"},
        csrf_path="/projects",
    )
    assert resp.status_code == 404

    resp = await post_form(
        client,
        f"/u/alice/alice-public/links/{link.id}/delete",
        {},
        csrf_path="/projects",
    )
    assert resp.status_code == 404

    await db.refresh(link)
    assert link.title == "Source"


async def test_guest_can_view_links_tab_on_public_project(client, db):
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
    db.add(
        ProjectLink(
            project_id=project.id,
            title="Source",
            url="https://github.com/alice/bench",
            link_type=LinkType.github,
        )
    )
    await db.commit()

    resp = await client.get("/u/alice/public-bench/links")
    assert resp.status_code == 200
    assert "Source" in resp.text
    assert "github.com/alice/bench" in resp.text
    # No edit affordance for guests
    assert "/edit" not in resp.text.split("Links")[-1] or "links/" not in resp.text.split("Edit")[0]


async def test_guest_cannot_view_links_on_private_project(client, db):
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
    db.add(
        ProjectLink(
            project_id=project.id,
            title="Secret",
            url="https://example.com",
            link_type=LinkType.other,
        )
    )
    await db.commit()

    resp = await client.get("/u/alice/private-bench/links")
    assert resp.status_code == 404


async def test_guest_new_link_form_redirects_to_login(client, db):
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

    resp = await client.get("/u/alice/bench/links/new")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


# ---------- ordering + cascade ---------- #


async def test_links_render_in_sort_order_then_created(client, db):
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
            ProjectLink(
                project_id=project.id,
                title="link-alpha",
                url="https://a.example",
                link_type=LinkType.other,
                sort_order=0,
                created_at=base,
            ),
            ProjectLink(
                project_id=project.id,
                title="link-bravo",
                url="https://b.example",
                link_type=LinkType.other,
                sort_order=0,
                created_at=base + timedelta(hours=1),
            ),
            ProjectLink(
                project_id=project.id,
                title="link-charlie",
                url="https://c.example",
                link_type=LinkType.other,
                sort_order=-1,
                created_at=base + timedelta(hours=2),
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/bench/links")
    body = resp.text
    # charlie (sort_order=-1) comes first; alpha (earlier created_at) before bravo.
    assert (
        body.index("link-charlie")
        < body.index("link-alpha")
        < body.index("link-bravo")
    )


async def test_deleting_project_cascades_to_links(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.flush()
    db.add(
        ProjectLink(
            project_id=project.id,
            title="Doomed",
            url="https://example.com",
            link_type=LinkType.other,
        )
    )
    await db.commit()

    await login(client, "alice")
    await post_form(
        client,
        "/u/alice/bench/delete",
        {},
        csrf_path="/projects",
    )

    remaining = (await db.execute(select(ProjectLink))).scalars().all()
    assert remaining == []


# ---------- tab integration ---------- #


# ---------- reorder ---------- #


async def test_new_link_gets_sort_order_one_past_current_max(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    # Seed a couple of existing links with gapped sort_orders — simulating
    # prior reorders.
    db.add_all(
        [
            ProjectLink(
                project_id=project.id,
                title="existing-a",
                url="https://a.example",
                link_type=LinkType.other,
                sort_order=3,
            ),
            ProjectLink(
                project_id=project.id,
                title="existing-b",
                url="https://b.example",
                link_type=LinkType.other,
                sort_order=7,
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    await post_form(
        client,
        "/u/alice/bench/links",
        {"title": "new one", "url": "https://c.example", "link_type": "other"},
        csrf_path="/u/alice/bench",
    )
    new_link = (
        await db.execute(
            select(ProjectLink).where(ProjectLink.title == "new one")
        )
    ).scalar_one()
    # One past the largest existing (7) so the new link appends at the bottom.
    assert new_link.sort_order == 8


async def test_reorder_rewrites_sort_order_in_submitted_sequence(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    a = ProjectLink(
        project_id=project.id,
        title="a",
        url="https://a.example",
        link_type=LinkType.other,
        sort_order=0,
    )
    b = ProjectLink(
        project_id=project.id,
        title="b",
        url="https://b.example",
        link_type=LinkType.other,
        sort_order=1,
    )
    c = ProjectLink(
        project_id=project.id,
        title="c",
        url="https://c.example",
        link_type=LinkType.other,
        sort_order=2,
    )
    db.add_all([a, b, c])
    await db.commit()

    await login(client, "alice")
    # Drag c to the top, then a, then b.
    resp = await post_form(
        client,
        "/u/alice/bench/links/reorder",
        {"link_ids": [str(c.id), str(a.id), str(b.id)]},
        csrf_path="/u/alice/bench/links",
    )
    assert resp.status_code == 204

    await db.refresh(a)
    await db.refresh(b)
    await db.refresh(c)
    assert c.sort_order == 0
    assert a.sort_order == 1
    assert b.sort_order == 2

    # Subsequent render reflects the new order.
    resp = await client.get("/u/alice/bench/links")
    body = resp.text
    assert body.index("https://c.example") < body.index("https://a.example") < body.index("https://b.example")


async def test_reorder_ignores_ids_from_other_projects(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    bench = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    shelf = Project(
        user_id=user.id,
        title="Shelf",
        slug="shelf",
        status=ProjectStatus.in_progress,
    )
    db.add_all([bench, shelf])
    await db.flush()
    bench_link = ProjectLink(
        project_id=bench.id,
        title="bench-link",
        url="https://a.example",
        link_type=LinkType.other,
        sort_order=0,
    )
    shelf_link = ProjectLink(
        project_id=shelf.id,
        title="shelf-link",
        url="https://b.example",
        link_type=LinkType.other,
        sort_order=5,
    )
    db.add_all([bench_link, shelf_link])
    await db.commit()

    await login(client, "alice")
    # Try to mix in an ID from another project — should be ignored silently.
    resp = await post_form(
        client,
        "/u/alice/bench/links/reorder",
        {"link_ids": [str(shelf_link.id), str(bench_link.id)]},
        csrf_path="/u/alice/bench/links",
    )
    assert resp.status_code == 204

    await db.refresh(shelf_link)
    await db.refresh(bench_link)
    # shelf_link untouched — its original sort_order preserved
    assert shelf_link.sort_order == 5
    # bench_link gets index 1 (shelf_link was at index 0 but skipped)
    assert bench_link.sort_order == 1


async def test_reorder_tolerates_garbage_ids(client, db):
    """Non-UUID strings get filtered rather than 400-ing — a single bad
    id from client state shouldn't drop the entire reorder."""
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    a = ProjectLink(
        project_id=project.id,
        title="a",
        url="https://a.example",
        link_type=LinkType.other,
        sort_order=0,
    )
    b = ProjectLink(
        project_id=project.id,
        title="b",
        url="https://b.example",
        link_type=LinkType.other,
        sort_order=1,
    )
    db.add_all([a, b])
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        "/u/alice/bench/links/reorder",
        {"link_ids": ["not-a-uuid", str(b.id), "also-bad", str(a.id)]},
        csrf_path="/u/alice/bench/links",
    )
    assert resp.status_code == 204
    await db.refresh(a)
    await db.refresh(b)
    # b ended up at index 0 (after the bad id was dropped),
    # a ended up at index 1.
    assert b.sort_order == 0
    assert a.sort_order == 1


async def test_reorder_requires_owner(client, db):
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
    link = ProjectLink(
        project_id=project.id,
        title="a",
        url="https://a.example",
        link_type=LinkType.other,
        sort_order=0,
    )
    db.add(link)
    await db.commit()

    await login(client, "bob")
    resp = await post_form(
        client,
        "/u/alice/public/links/reorder",
        {"link_ids": [str(link.id)]},
        csrf_path="/projects",
    )
    # Username mismatch → 404 (not 403, to avoid confirming the project
    # exists to a non-owner attacker).
    assert resp.status_code == 404

    await db.refresh(link)
    assert link.sort_order == 0


async def test_reorder_requires_login(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Public",
        slug="public",
        status=ProjectStatus.in_progress,
        is_public=True,
    )
    db.add(project)
    await db.flush()
    link = ProjectLink(
        project_id=project.id,
        title="a",
        url="https://a.example",
        link_type=LinkType.other,
        sort_order=0,
    )
    db.add(link)
    await db.commit()

    # No login — bare POST
    resp = await client.post(
        "/u/alice/public/links/reorder",
        data={"link_ids": str(link.id)},
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


async def test_links_template_includes_sortable_wiring_for_owner(client, db):
    """Confirms the drag handles + Sortable init script ship on the links
    tab when viewed by the owner (but not for visitors)."""
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Public",
        slug="public",
        status=ProjectStatus.in_progress,
        is_public=True,
    )
    db.add(project)
    await db.flush()
    db.add(
        ProjectLink(
            project_id=project.id,
            title="a",
            url="https://a.example",
            link_type=LinkType.other,
            sort_order=0,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/public/links")
    assert "data-links-sortable" in resp.text
    assert "data-reorder-url" in resp.text
    assert "drag-handle" in resp.text
    assert "sortable.min.js" in resp.text


async def test_links_template_has_no_sortable_wiring_for_guests(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Public",
        slug="public",
        status=ProjectStatus.in_progress,
        is_public=True,
    )
    db.add(project)
    await db.flush()
    db.add(
        ProjectLink(
            project_id=project.id,
            title="a",
            url="https://a.example",
            link_type=LinkType.other,
            sort_order=0,
        )
    )
    await db.commit()

    # Not logged in — viewer is a guest.
    resp = await client.get("/u/alice/public/links")
    assert resp.status_code == 200
    assert "data-links-sortable" not in resp.text
    assert "drag-handle" not in resp.text
    assert "sortable.min.js" not in resp.text


async def test_links_tab_appears_in_project_nav(client, db):
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
    resp = await client.get("/u/alice/bench")
    assert resp.status_code == 200
    # Links tab link is rendered in the project tab bar
    assert 'href="/u/alice/bench/links"' in resp.text
    # On the links tab itself, it's marked active
    resp = await client.get("/u/alice/bench/links")
    # Locate the Links nav anchor and check aria-current
    anchor_start = resp.text.index('href="/u/alice/bench/links"')
    anchor_block = resp.text[anchor_start:anchor_start + 300]
    assert 'aria-current="page"' in anchor_block
