"""Tests for the `files/…` typeahead wiring.

Client-side typeahead behaviour (keyboard nav, insertion, filtering) lives
in JS and isn't unit-tested here — instead we verify the server-side
contract: the file index JSON is rendered into the pages that get a
project-scoped editor, and the bio editor stays untouched.
"""

import json
import re
import uuid

from benchlog.files import get_project_file_index
from benchlog.models import FileVersion, Project, ProjectFile, ProjectStatus
from tests.conftest import login, make_user


async def _seed_project(db, user, **overrides) -> Project:
    defaults = {
        "user_id": user.id,
        "title": "Bench",
        "slug": "bench",
        "description": "hi",
        "status": ProjectStatus.in_progress,
        "is_public": True,
    }
    defaults.update(overrides)
    project = Project(**defaults)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _seed_file(
    db,
    project: Project,
    *,
    path: str = "",
    filename: str = "notes.txt",
    mime_type: str = "text/plain",
    with_version: bool = True,
) -> ProjectFile:
    """Insert a ProjectFile row (optionally with a FileVersion wired as current).

    Direct DB setup is cheaper than round-tripping through the upload
    endpoint for index-serialization tests. `with_version=False` leaves
    `current_version_id` NULL — used to verify the helper drops orphans.
    """
    pf = ProjectFile(project_id=project.id, path=path, filename=filename)
    db.add(pf)
    await db.flush()  # so pf.id is available
    if with_version:
        fv = FileVersion(
            file_id=pf.id,
            version_number=1,
            storage_path=f"files/{pf.id}/1",
            original_name=filename,
            size_bytes=1,
            mime_type=mime_type,
            checksum="0" * 64,
        )
        db.add(fv)
        await db.flush()
        pf.current_version_id = fv.id
    await db.commit()
    await db.refresh(pf)
    return pf


def _extract_file_index(html: str, attr: str = "data-toastui-file-index") -> list:
    """Pull and JSON-decode the first `data-…-file-index` attribute value.

    Tolerates both single- and double-quoted attribute values — our Jinja
    `|tojson` filter emits single quotes on the outside, but a future
    template change could flip that.
    """
    m = re.search(rf"{attr}='([^']*)'", html) or re.search(
        rf'{attr}="([^"]*)"', html
    )
    if not m:
        return None
    return json.loads(m.group(1))


# ---------- helper semantics ---------- #


async def test_file_index_helper_excludes_orphan_files(db):
    user = await make_user(db)
    project = await _seed_project(db, user)
    await _seed_file(db, project, filename="good.txt")
    await _seed_file(db, project, filename="orphan.txt", with_version=False)

    index = await get_project_file_index(db, project.id)
    names = [e["filename"] for e in index]
    assert names == ["good.txt"]


async def test_file_index_helper_sorts_by_path_then_filename(db):
    user = await make_user(db)
    project = await _seed_project(db, user)
    # Insert in an order different from the expected output so the sort
    # is actually doing work.
    await _seed_file(db, project, path="models", filename="zeta.stl")
    await _seed_file(db, project, path="", filename="readme.md")
    await _seed_file(db, project, path="models", filename="alpha.stl")
    await _seed_file(db, project, path="docs", filename="intro.md")

    index = await get_project_file_index(db, project.id)
    got = [(e["path"], e["filename"]) for e in index]
    assert got == [
        ("", "readme.md"),
        ("docs", "intro.md"),
        ("models", "alpha.stl"),
        ("models", "zeta.stl"),
    ]


async def test_file_index_helper_caps_at_500(db):
    user = await make_user(db)
    project = await _seed_project(db, user)
    # Create 502 files with a stable sort prefix so the cap is observable.
    # Bulk-insert via add_all to keep this fast.
    pfs = [
        ProjectFile(
            project_id=project.id,
            path="",
            filename=f"f{i:04d}.txt",
        )
        for i in range(502)
    ]
    db.add_all(pfs)
    await db.flush()
    fvs = [
        FileVersion(
            file_id=pf.id,
            version_number=1,
            storage_path=f"files/{pf.id}/1",
            original_name=pf.filename,
            size_bytes=1,
            mime_type="text/plain",
            checksum="0" * 64,
        )
        for pf in pfs
    ]
    db.add_all(fvs)
    await db.flush()
    for pf, fv in zip(pfs, fvs):
        pf.current_version_id = fv.id
    await db.commit()

    index = await get_project_file_index(db, project.id)
    assert len(index) == 500
    # First entry should be the earliest-sorted filename, confirming the
    # truncation happens after sort (not randomly).
    assert index[0]["filename"] == "f0000.txt"


async def test_file_index_includes_is_image_flag(db):
    user = await make_user(db)
    project = await _seed_project(db, user)
    await _seed_file(db, project, filename="hero.png", mime_type="image/png")
    await _seed_file(db, project, filename="notes.txt", mime_type="text/plain")

    index = await get_project_file_index(db, project.id)
    by_name = {e["filename"]: e for e in index}
    assert by_name["hero.png"]["is_image"] is True
    assert by_name["notes.txt"]["is_image"] is False


# ---------- template rendering ---------- #


async def test_project_detail_renders_file_index_for_owner(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _seed_project(db, alice)
    await _seed_file(db, project, path="", filename="notes.md")
    await _seed_file(db, project, path="imgs", filename="p.jpg", mime_type="image/jpeg")

    await login(client, "alice")
    resp = await client.get("/u/alice/bench")
    assert resp.status_code == 200
    # The inline-edit <form> carries the index via data-file-index so
    # description-edit.js can forward it onto the dynamic mount.
    index = _extract_file_index(resp.text, attr="data-file-index")
    assert index is not None, "data-file-index missing on inline-edit form"
    names = [(e["path"], e["filename"]) for e in index]
    assert ("", "notes.md") in names
    assert ("imgs", "p.jpg") in names


async def test_project_detail_omits_file_index_for_non_owners(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    project = await _seed_project(db, alice)
    await _seed_file(db, project, filename="notes.md")

    await login(client, "bob")
    resp = await client.get("/u/alice/bench")
    assert resp.status_code == 200
    # Guests don't get the inline-edit form at all — so no file index to
    # leak, and no editor to attach it to.
    assert "data-description-edit-form" not in resp.text
    assert "data-file-index" not in resp.text
    assert "data-toastui-file-index" not in resp.text


async def test_journal_tab_renders_file_index_with_parent_project_files(client, db):
    # File index rides on `[data-journal-section]` so the new-entry modal
    # and every inline editor on the page share one source of truth —
    # no per-editor refetch.
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _seed_project(db, alice)
    await _seed_file(db, project, path="models", filename="bracket.stl")

    from benchlog.models import JournalEntry

    db.add(
        JournalEntry(
            project_id=project.id, title=None, content="body", is_public=False
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/bench/journal")
    assert resp.status_code == 200
    index = _extract_file_index(resp.text, attr="data-file-index")
    assert index is not None
    names = [(e["path"], e["filename"]) for e in index]
    assert ("models", "bracket.stl") in names


async def test_bio_editor_has_no_file_index(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")
    resp = await client.get("/account")
    assert resp.status_code == 200
    # Account page mounts the bio editor but there's no project context,
    # so the typeahead must stay disabled there.
    assert "data-toastui-file-index" not in resp.text


async def test_new_project_form_renders_empty_file_index(client, db):
    """The create form is rendered with no project yet, so the index is [].

    The attribute should still be present with an empty array so the JS
    path is uniform — "enable typeahead, no matches yet".
    """
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")
    resp = await client.get("/projects/new")
    assert resp.status_code == 200
    index = _extract_file_index(resp.text)
    assert index == []


async def test_file_index_helper_unknown_project_returns_empty(db):
    # Sanity: no rows for an unrelated id → empty list, not an error.
    out = await get_project_file_index(db, uuid.uuid4())
    assert out == []
