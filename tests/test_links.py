"""Tests for link helpers, sections, links, reorder, and modal endpoints."""

from sqlalchemy import select

from benchlog.links import (
    normalize_url,
    section_name_key,
    next_section_sort_order,
    next_link_sort_order,
)
from benchlog.models import LinkSection, Project, ProjectLink, ProjectStatus
from tests.conftest import login, make_user, post_form


# ---------- URL normalization (unchanged) ---------- #


def test_normalize_url_adds_https_when_missing_scheme():
    assert normalize_url("github.com/alice/proj") == "https://github.com/alice/proj"


def test_normalize_url_preserves_http_and_https():
    assert normalize_url("http://example.com") == "http://example.com"
    assert normalize_url("https://example.com/path") == "https://example.com/path"


def test_normalize_url_accepts_non_web_schemes():
    assert normalize_url("mailto:alice@example.com") == "mailto:alice@example.com"
    assert normalize_url("ssh://server.example") == "ssh://server.example"


def test_normalize_url_blocks_xss_schemes():
    assert normalize_url("javascript:alert(1)") == ""
    assert normalize_url("JavaScript:alert(1)") == ""
    assert normalize_url("data:text/html,<script>alert(1)</script>") == ""


def test_normalize_url_rejects_empty_and_no_host():
    assert normalize_url("") == ""
    assert normalize_url("   ") == ""
    assert normalize_url("https://") == ""


# ---------- section name normalization ---------- #


def test_section_name_key_lowercases_and_strips():
    assert section_name_key(" Inspiration ") == "inspiration"
    assert section_name_key("INSPIRATION") == "inspiration"
    # Internal whitespace is preserved (just trim ends + lowercase).
    assert section_name_key("Cool Refs") == "cool refs"


def test_section_name_key_empty_for_empty_input():
    assert section_name_key("") == ""
    assert section_name_key("    ") == ""


# ---------- config ---------- #


def test_settings_metadata_fetch_allow_private_default_false(monkeypatch):
    """The flag must default to False — the safe choice for any
    multi-user deployment. Self-hosted single-user instances flip it on
    via env to enable previews of LAN URLs.

    Bypass both the developer's local `.env` file and any pre-set process
    env var so this test asserts the class default rather than whatever
    the maintainer has configured locally.
    """
    from benchlog.config import Settings

    monkeypatch.delenv("BENCHLOG_METADATA_FETCH_ALLOW_PRIVATE", raising=False)
    assert Settings(_env_file=None).metadata_fetch_allow_private is False


# ---------- section CRUD ---------- #


async def test_owner_creates_section(client, db):
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
        "/u/alice/bench/links/sections",
        {"name": "Inspiration"},
        csrf_path="/u/alice/bench/links",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/bench/links"

    section = (await db.execute(select(LinkSection))).scalar_one()
    assert section.name == "Inspiration"
    assert section.name_key == "inspiration"
    assert section.sort_order == 0


async def test_create_section_rejects_blank_name(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    )
    await db.commit()
    await login(client, "alice")
    resp = await post_form(
        client,
        "/u/alice/b/links/sections",
        {"name": "   "},
        csrf_path="/u/alice/b/links",
    )
    assert resp.status_code == 400
    assert (await db.execute(select(LinkSection))).scalars().all() == []


async def test_create_section_rejects_case_insensitive_duplicate(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    db.add(project)
    await db.flush()
    db.add(
        LinkSection(
            project_id=project.id, name="Inspiration", name_key="inspiration",
        )
    )
    await db.commit()
    await login(client, "alice")
    resp = await post_form(
        client,
        "/u/alice/b/links/sections",
        {"name": "INSPIRATION"},
        csrf_path="/u/alice/b/links",
    )
    assert resp.status_code == 400
    rows = (await db.execute(select(LinkSection))).scalars().all()
    assert len(rows) == 1


async def test_create_section_requires_owner(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    db.add(
        Project(
            user_id=alice.id,
            title="A",
            slug="a",
            status=ProjectStatus.in_progress,
            is_public=True,
        )
    )
    await db.commit()
    await login(client, "bob")
    resp = await post_form(
        client,
        "/u/alice/a/links/sections",
        {"name": "X"},
        csrf_path="/projects",
    )
    assert resp.status_code == 404


async def test_owner_renames_section(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    db.add(project)
    await db.flush()
    section = LinkSection(
        project_id=project.id, name="Old", name_key="old", sort_order=0
    )
    db.add(section)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/b/links/sections/{section.id}/rename",
        {"name": "New Name"},
        csrf_path="/u/alice/b/links",
    )
    assert resp.status_code == 302
    await db.refresh(section)
    assert section.name == "New Name"
    assert section.name_key == "new name"


async def test_rename_section_rejects_dup_against_other_section(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    db.add(project)
    await db.flush()
    db.add_all(
        [
            LinkSection(project_id=project.id, name="One", name_key="one", sort_order=0),
            LinkSection(project_id=project.id, name="Two", name_key="two", sort_order=1),
        ]
    )
    await db.commit()
    two = (
        await db.execute(select(LinkSection).where(LinkSection.name_key == "two"))
    ).scalar_one()
    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/b/links/sections/{two.id}/rename",
        {"name": "ONE"},
        csrf_path="/u/alice/b/links",
    )
    assert resp.status_code == 400
    await db.refresh(two)
    assert two.name == "Two"


async def test_owner_deletes_section_and_its_links(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    db.add(project)
    await db.flush()
    section = LinkSection(
        project_id=project.id, name="Doomed", name_key="doomed", sort_order=0
    )
    db.add(section)
    await db.flush()
    db.add_all(
        [
            ProjectLink(section_id=section.id, title="One", url="https://a.example", sort_order=0),
            ProjectLink(section_id=section.id, title="Two", url="https://b.example", sort_order=1),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/b/links/sections/{section.id}/delete",
        {},
        csrf_path="/u/alice/b/links",
    )
    assert resp.status_code == 302
    assert (await db.execute(select(LinkSection))).scalars().all() == []
    assert (await db.execute(select(ProjectLink))).scalars().all() == []


async def test_owner_reorders_sections(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    db.add(project)
    await db.flush()
    a = LinkSection(project_id=project.id, name="A", name_key="a", sort_order=0)
    b = LinkSection(project_id=project.id, name="B", name_key="b", sort_order=1)
    c = LinkSection(project_id=project.id, name="C", name_key="c", sort_order=2)
    db.add_all([a, b, c])
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        "/u/alice/b/links/sections/reorder",
        {"section_ids": [str(c.id), str(a.id), str(b.id)]},
        csrf_path="/u/alice/b/links",
    )
    assert resp.status_code == 204
    await db.refresh(a)
    await db.refresh(b)
    await db.refresh(c)
    assert (c.sort_order, a.sort_order, b.sort_order) == (0, 1, 2)


async def test_section_routes_require_owner(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    project = Project(
        user_id=alice.id,
        title="A",
        slug="a",
        status=ProjectStatus.in_progress,
        is_public=True,
    )
    db.add(project)
    await db.flush()
    section = LinkSection(
        project_id=project.id, name="X", name_key="x", sort_order=0
    )
    db.add(section)
    await db.commit()

    await login(client, "bob")
    for path, body in [
        (f"/u/alice/a/links/sections/{section.id}/rename", {"name": "Y"}),
        (f"/u/alice/a/links/sections/{section.id}/delete", {}),
        ("/u/alice/a/links/sections/reorder", {"section_ids": [str(section.id)]}),
    ]:
        resp = await post_form(client, path, body, csrf_path="/projects")
        assert resp.status_code == 404, f"expected 404 for {path}"


# ---------- link create / edit / delete ---------- #


async def test_owner_creates_link_in_existing_section(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    db.add(project)
    await db.flush()
    section = LinkSection(
        project_id=project.id, name="Refs", name_key="refs", sort_order=0
    )
    db.add(section)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        "/u/alice/b/links",
        {
            "title": "Source",
            "url": "https://github.com/alice/b",
            "section_name": "Refs",
            "note": "Where the code lives",
            "og_title": "alice/b",
            "og_description": "Bench experiment",
            "og_image_url": "https://avatars.example/b.png",
            "og_site_name": "GitHub",
            "favicon_url": "https://github.com/favicon.ico",
        },
        csrf_path="/u/alice/b/links",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/b/links"

    link = (await db.execute(select(ProjectLink))).scalar_one()
    assert link.section_id == section.id
    assert link.title == "Source"
    assert link.note == "Where the code lives"
    assert link.og_title == "alice/b"
    assert link.og_image_url == "https://avatars.example/b.png"
    assert link.og_site_name == "GitHub"


async def test_create_link_creates_new_section_when_name_unknown(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    )
    await db.commit()

    await login(client, "alice")
    await post_form(
        client,
        "/u/alice/b/links",
        {
            "title": "Brand new",
            "url": "https://x.example",
            "section_name": "Brand New Bucket",
        },
        csrf_path="/u/alice/b/links",
    )
    section = (await db.execute(select(LinkSection))).scalar_one()
    assert section.name == "Brand New Bucket"
    link = (await db.execute(select(ProjectLink))).scalar_one()
    assert link.section_id == section.id


async def test_create_link_rejects_blank_section_name(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    )
    await db.commit()
    await login(client, "alice")
    resp = await post_form(
        client,
        "/u/alice/b/links",
        {"title": "X", "url": "https://x.example", "section_name": "  "},
        csrf_path="/u/alice/b/links",
    )
    assert resp.status_code == 400
    assert (await db.execute(select(ProjectLink))).scalars().all() == []


async def test_create_link_rejects_note_over_280(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    )
    await db.commit()
    await login(client, "alice")
    note = "x" * 281
    resp = await post_form(
        client,
        "/u/alice/b/links",
        {
            "title": "X",
            "url": "https://x.example",
            "section_name": "Refs",
            "note": note,
        },
        csrf_path="/u/alice/b/links",
    )
    assert resp.status_code == 400


async def test_owner_can_edit_link_and_change_section(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    db.add(project)
    await db.flush()
    s1 = LinkSection(project_id=project.id, name="One", name_key="one", sort_order=0)
    s2 = LinkSection(project_id=project.id, name="Two", name_key="two", sort_order=1)
    db.add_all([s1, s2])
    await db.flush()
    link = ProjectLink(
        section_id=s1.id, title="Old", url="https://old.example", sort_order=0
    )
    db.add(link)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/b/links/{link.id}",
        {
            "title": "New",
            "url": "https://new.example",
            "section_name": "Two",
            "note": "renamed",
        },
        csrf_path="/u/alice/b/links",
    )
    assert resp.status_code == 302
    await db.refresh(link)
    assert link.title == "New"
    assert link.url == "https://new.example"
    assert link.section_id == s2.id
    assert link.note == "renamed"


async def test_owner_can_delete_link(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    db.add(project)
    await db.flush()
    section = LinkSection(
        project_id=project.id, name="X", name_key="x", sort_order=0
    )
    db.add(section)
    await db.flush()
    link = ProjectLink(section_id=section.id, title="X", url="https://x.example")
    db.add(link)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/b/links/{link.id}/delete",
        {},
        csrf_path="/u/alice/b/links",
    )
    assert resp.status_code == 302
    assert (await db.execute(select(ProjectLink))).scalars().all() == []


async def test_link_routes_require_owner(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    project = Project(
        user_id=alice.id,
        title="A",
        slug="a",
        status=ProjectStatus.in_progress,
        is_public=True,
    )
    db.add(project)
    await db.flush()
    section = LinkSection(
        project_id=project.id, name="X", name_key="x", sort_order=0
    )
    db.add(section)
    await db.flush()
    link = ProjectLink(section_id=section.id, title="X", url="https://x.example")
    db.add(link)
    await db.commit()

    await login(client, "bob")
    paths = [
        ("/u/alice/a/links", {"title": "Y", "url": "https://y", "section_name": "Z"}),
        (f"/u/alice/a/links/{link.id}", {"title": "Y", "url": "https://y", "section_name": "X"}),
        (f"/u/alice/a/links/{link.id}/delete", {}),
    ]
    for path, body in paths:
        resp = await post_form(client, path, body, csrf_path="/projects")
        assert resp.status_code == 404, f"expected 404 for {path}"


async def test_edit_link_json_returns_full_state(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    db.add(project)
    await db.flush()
    section = LinkSection(
        project_id=project.id, name="Refs", name_key="refs", sort_order=0
    )
    db.add(section)
    await db.flush()
    link = ProjectLink(
        section_id=section.id,
        title="Source",
        url="https://github.com/alice/b",
        note="my repo",
        og_title="alice/b",
        og_site_name="GitHub",
        sort_order=0,
    )
    db.add(link)
    await db.commit()

    await login(client, "alice")
    resp = await client.get(f"/u/alice/b/links/{link.id}/edit")
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Source"
    assert body["section_name"] == "Refs"
    assert body["og_title"] == "alice/b"
    assert body["note"] == "my repo"


# ---------- link reorder ---------- #


import json as _json  # noqa: E402


async def test_reorder_links_within_section(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    db.add(project)
    await db.flush()
    section = LinkSection(
        project_id=project.id, name="X", name_key="x", sort_order=0
    )
    db.add(section)
    await db.flush()
    a = ProjectLink(section_id=section.id, title="a", url="https://a.example", sort_order=0)
    b = ProjectLink(section_id=section.id, title="b", url="https://b.example", sort_order=1)
    c = ProjectLink(section_id=section.id, title="c", url="https://c.example", sort_order=2)
    db.add_all([a, b, c])
    await db.commit()

    await login(client, "alice")
    payload = _json.dumps(
        [
            {"link_id": str(c.id), "section_id": str(section.id), "position": 0},
            {"link_id": str(a.id), "section_id": str(section.id), "position": 1},
            {"link_id": str(b.id), "section_id": str(section.id), "position": 2},
        ]
    )
    resp = await post_form(
        client,
        "/u/alice/b/links/reorder",
        {"payload": payload},
        csrf_path="/u/alice/b/links",
    )
    assert resp.status_code == 204
    await db.refresh(a)
    await db.refresh(b)
    await db.refresh(c)
    assert (c.sort_order, a.sort_order, b.sort_order) == (0, 1, 2)


async def test_reorder_moves_link_across_sections(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    db.add(project)
    await db.flush()
    s1 = LinkSection(project_id=project.id, name="One", name_key="one", sort_order=0)
    s2 = LinkSection(project_id=project.id, name="Two", name_key="two", sort_order=1)
    db.add_all([s1, s2])
    await db.flush()
    a = ProjectLink(section_id=s1.id, title="a", url="https://a", sort_order=0)
    b = ProjectLink(section_id=s2.id, title="b", url="https://b", sort_order=0)
    db.add_all([a, b])
    await db.commit()

    await login(client, "alice")
    payload = _json.dumps(
        [
            {"link_id": str(b.id), "section_id": str(s2.id), "position": 0},
            {"link_id": str(a.id), "section_id": str(s2.id), "position": 1},
        ]
    )
    resp = await post_form(
        client,
        "/u/alice/b/links/reorder",
        {"payload": payload},
        csrf_path="/u/alice/b/links",
    )
    assert resp.status_code == 204
    await db.refresh(a)
    await db.refresh(b)
    assert a.section_id == s2.id
    assert (b.sort_order, a.sort_order) == (0, 1)


async def test_reorder_ignores_links_from_other_projects(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bench = Project(
        user_id=alice.id, title="Bench", slug="bench",
        status=ProjectStatus.in_progress,
    )
    shelf = Project(
        user_id=alice.id, title="Shelf", slug="shelf",
        status=ProjectStatus.in_progress,
    )
    db.add_all([bench, shelf])
    await db.flush()
    bs = LinkSection(project_id=bench.id, name="X", name_key="x", sort_order=0)
    ss = LinkSection(project_id=shelf.id, name="Y", name_key="y", sort_order=0)
    db.add_all([bs, ss])
    await db.flush()
    bl = ProjectLink(section_id=bs.id, title="b", url="https://b.example", sort_order=0)
    sl = ProjectLink(section_id=ss.id, title="s", url="https://s.example", sort_order=5)
    db.add_all([bl, sl])
    await db.commit()

    await login(client, "alice")
    payload = _json.dumps(
        [
            {"link_id": str(sl.id), "section_id": str(bs.id), "position": 0},
            {"link_id": str(bl.id), "section_id": str(bs.id), "position": 1},
        ]
    )
    resp = await post_form(
        client,
        "/u/alice/bench/links/reorder",
        {"payload": payload},
        csrf_path="/u/alice/bench/links",
    )
    assert resp.status_code == 204
    await db.refresh(bl)
    await db.refresh(sl)
    assert sl.section_id == ss.id
    assert sl.sort_order == 5
    assert bl.sort_order == 1


async def test_reorder_tolerates_garbage_payload(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id, title="B", slug="b", status=ProjectStatus.in_progress,
    )
    db.add(project)
    await db.flush()
    section = LinkSection(
        project_id=project.id, name="X", name_key="x", sort_order=0
    )
    db.add(section)
    await db.flush()
    a = ProjectLink(section_id=section.id, title="a", url="https://a", sort_order=0)
    db.add(a)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        "/u/alice/b/links/reorder",
        {"payload": "not-json"},
        csrf_path="/u/alice/b/links",
    )
    assert resp.status_code == 204
    await db.refresh(a)
    assert a.sort_order == 0


# ---------- metadata fetch endpoints ---------- #


async def test_fetch_metadata_endpoint_returns_canned_metadata(client, db, monkeypatch):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    )
    await db.commit()

    async def fake(url):
        return {
            "title": "Hello",
            "description": "World",
            "image_url": "https://cdn/x.png",
            "site_name": "Example",
            "favicon_url": "https://example.com/favicon.ico",
            "warning": None,
        }

    from benchlog.routes import links as links_routes
    monkeypatch.setattr(links_routes, "_metadata_fetcher", fake)

    await login(client, "alice")
    resp = await post_form(
        client,
        "/u/alice/b/links/fetch-metadata",
        {"url": "https://example.com/x"},
        csrf_path="/u/alice/b/links",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Hello"
    assert body["site_name"] == "Example"
    assert body["warning"] is None


async def test_fetch_metadata_endpoint_requires_owner(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    db.add(
        Project(
            user_id=alice.id, title="A", slug="a",
            status=ProjectStatus.in_progress, is_public=True,
        )
    )
    await db.commit()
    await login(client, "bob")
    resp = await post_form(
        client,
        "/u/alice/a/links/fetch-metadata",
        {"url": "https://example.com"},
        csrf_path="/projects",
    )
    assert resp.status_code == 404


async def test_refetch_metadata_persists_to_link(client, db, monkeypatch):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(user_id=user.id, title="B", slug="b", status=ProjectStatus.idea)
    db.add(project)
    await db.flush()
    section = LinkSection(
        project_id=project.id, name="X", name_key="x", sort_order=0
    )
    db.add(section)
    await db.flush()
    link = ProjectLink(
        section_id=section.id, title="Old", url="https://example.com/x", sort_order=0
    )
    db.add(link)
    await db.commit()

    async def fake(url):
        return {
            "title": "Fresh title",
            "description": "Fresh desc",
            "image_url": "https://cdn/fresh.png",
            "site_name": "Example",
            "favicon_url": "https://example.com/favicon.ico",
            "warning": None,
        }

    from benchlog.routes import links as links_routes
    monkeypatch.setattr(links_routes, "_metadata_fetcher", fake)

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/b/links/{link.id}/refetch-metadata",
        {},
        csrf_path="/u/alice/b/links",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Fresh title"
    await db.refresh(link)
    assert link.og_title == "Fresh title"
    assert link.metadata_fetched_at is not None
