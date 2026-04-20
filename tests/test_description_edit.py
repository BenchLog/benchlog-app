"""Tests for the project description: markdown rendering on the detail
page, owner-only inline edit UI, and the AJAX /description endpoint."""

import json

from sqlalchemy import select

from benchlog.models import Project, ProjectStatus
from tests.conftest import csrf_token, login, make_user, post_form


async def _seed_project(db, user, **overrides) -> Project:
    defaults = {
        "user_id": user.id,
        "title": "Bench",
        "slug": "bench",
        "description": "v1 draft",
        "status": ProjectStatus.in_progress,
        "is_public": True,
    }
    defaults.update(overrides)
    project = Project(**defaults)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def test_description_renders_as_markdown(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    await _seed_project(db, alice, description="**bold** and _italic_")

    # Log in as a non-owner so the inline-edit form (which carries the raw
    # source in `data-raw-description`) isn't emitted and we can cleanly
    # assert the rendered block is the markdown-rendered output.
    await login(client, "bob")
    resp = await client.get("/u/alice/bench")
    assert resp.status_code == 200
    assert "<strong>bold</strong>" in resp.text
    assert "<em>italic</em>" in resp.text
    assert "**bold**" not in resp.text


async def test_description_rewrites_file_links_to_canonical_url(client, db):
    # `files/<path>/<filename>` links must resolve to the file detail page
    # using the UUID — not the filename (which would 422 on the UUID-typed
    # path param) and not a relative `files/...` (which would resolve
    # against the current URL and produce different links on different
    # pages). Regression guard for a bug we hit in the wild.
    import uuid as _uuid
    from benchlog.models import FileVersion, ProjectFile

    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _seed_project(
        db, alice, description="See [the spool](files/3d-files/spool.stl)."
    )
    pf = ProjectFile(
        id=_uuid.uuid4(),
        project_id=project.id,
        path="3d-files",
        filename="spool.stl",
    )
    db.add(pf)
    await db.flush()
    fv = FileVersion(
        file_id=pf.id,
        version_number=1,
        storage_path=f"files/{pf.id}/1.stl",
        original_name="spool.stl",
        size_bytes=100,
        mime_type="model/stl",
        checksum="abc",
    )
    db.add(fv)
    await db.flush()
    pf.current_version_id = fv.id
    await db.commit()

    resp = await client.get("/u/alice/bench")
    assert resp.status_code == 200
    # Canonical: absolute path + slug + file UUID (detail page, not /download).
    assert f'href="/u/alice/bench/files/{pf.id}"' in resp.text
    # Must NOT contain the relative filename-based href that was breaking
    # the click-through.
    assert 'href="files/3d-files/spool.stl"' not in resp.text


async def test_description_inline_edit_button_visible_to_owner_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    await _seed_project(db, alice)

    # Owner sees the Edit affordance and the inline form.
    await login(client, "alice")
    resp = await client.get("/u/alice/bench")
    assert resp.status_code == 200
    assert "data-description-edit" in resp.text
    assert "data-description-edit-form" in resp.text

    # Non-owner viewing the same public project does not.
    await login(client, "bob")
    resp = await client.get("/u/alice/bench")
    assert resp.status_code == 200
    assert "data-description-edit" not in resp.text
    assert "data-description-edit-form" not in resp.text


async def test_description_endpoint_updates_and_returns_rendered_html(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _seed_project(db, alice, description="old")

    await login(client, "alice")
    token = await csrf_token(client, "/u/alice/bench")

    resp = await client.post(
        "/u/alice/bench/description",
        content=json.dumps({"description": "# new"}),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-CSRF-Token": token,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "<h1>new</h1>" in data["html"]

    await db.refresh(project)
    assert project.description == "# new"


async def test_description_endpoint_empty_string_clears(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _seed_project(db, alice, description="old")

    await login(client, "alice")
    token = await csrf_token(client, "/u/alice/bench")

    # Whitespace-only should also clear (mirrors the main edit path's
    # `.strip() or None` behaviour).
    resp = await client.post(
        "/u/alice/bench/description",
        content=json.dumps({"description": "   "}),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-CSRF-Token": token,
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"html": ""}

    await db.refresh(project)
    assert project.description is None


async def test_description_endpoint_owner_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    await _seed_project(db, alice)

    # Non-owner: JSON path is 404, not 403 (visibility framing).
    await login(client, "bob")
    token = await csrf_token(client, "/u/alice/bench")
    resp = await client.post(
        "/u/alice/bench/description",
        content=json.dumps({"description": "hijacked"}),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-CSRF-Token": token,
        },
    )
    assert resp.status_code == 404

    # Non-owner: form path is also 404.
    resp = await post_form(
        client,
        "/u/alice/bench/description",
        {"description": "hijacked"},
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 404

    # DB untouched.
    result = await db.execute(select(Project).where(Project.slug == "bench"))
    project = result.scalar_one()
    assert project.description == "v1 draft"


async def test_description_endpoint_form_post_redirects(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _seed_project(db, alice, description="old")

    await login(client, "alice")
    resp = await post_form(
        client,
        "/u/alice/bench/description",
        {"description": "from form"},
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/bench"

    await db.refresh(project)
    assert project.description == "from form"


async def test_description_endpoint_requires_csrf(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _seed_project(db, alice, description="old")

    await login(client, "alice")
    resp = await client.post(
        "/u/alice/bench/description",
        content=json.dumps({"description": "no csrf"}),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    # JSON-content CSRF failure returns JSON 403 from the middleware.
    assert resp.status_code == 403

    await db.refresh(project)
    assert project.description == "old"
