"""Whole-project export — /u/{u}/{s}/export returns a zip that round-trips
the project metadata + file blobs. Visibility of updates is honoured: a
guest on a public project only gets the public updates, owner gets all.
"""

import io
import json
import shutil
import zipfile

import pytest

from benchlog.config import settings
from benchlog.models import (
    LinkType,
    Project,
    ProjectLink,
    ProjectStatus,
    ProjectUpdate,
    Tag,
)
from benchlog.storage import get_storage
from tests.conftest import csrf_token, login, make_user


# ---------- helpers ---------- #


@pytest.fixture(autouse=True)
def _isolate_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_local_path", str(tmp_path / "files"))
    get_storage.cache_clear()
    yield
    get_storage.cache_clear()
    shutil.rmtree(tmp_path / "files", ignore_errors=True)


async def _upload(
    client, url: str, *, filename: str, content: bytes, mime: str = "text/plain",
    extra_form: dict | None = None, csrf_path: str = "/projects",
):
    token = await csrf_token(client, csrf_path)
    data = {"_csrf": token, **(extra_form or {})}
    files = {"upload": (filename, content, mime)}
    return await client.post(url, data=data, files=files)


def _unzip(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


async def _seed_rich_project(db, *, is_public: bool = True) -> Project:
    """Project with a tag, two updates (one public, one private), a link,
    and two files (one at root, one nested)."""
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Bench",
        slug="bench",
        description="A sturdy workbench for the garage.",
        status=ProjectStatus.in_progress,
        is_public=is_public,
    )
    db.add(project)
    await db.flush()
    tag = Tag(slug="woodwork")
    db.add(tag)
    await db.flush()
    from benchlog.models import ProjectTag
    db.add(ProjectTag(project_id=project.id, tag_id=tag.id))
    db.add_all(
        [
            ProjectUpdate(
                project_id=project.id,
                title="Day 1",
                content="Glued the tenons.",
                is_public=True,
            ),
            ProjectUpdate(
                project_id=project.id,
                title="Todo",
                content="Still need to sand.",
                is_public=False,
            ),
        ]
    )
    db.add(
        ProjectLink(
            project_id=project.id,
            title="Inspiration",
            url="https://example.com/bench",
            link_type=LinkType.website,
            sort_order=0,
        )
    )
    await db.commit()
    return project


# ---------- owner export ---------- #


async def test_owner_export_includes_every_update_and_every_file(client, db):
    await _seed_rich_project(db)
    await login(client, "alice")
    # Two files — one at root, one nested under "models".
    await _upload(
        client, "/u/alice/bench/files",
        filename="readme.md", content=b"# Bench\n\nRoot file.", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="widget.stl", content=b"solid teapot\n", mime="model/stl",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert 'filename="bench.zip"' in resp.headers["content-disposition"]

    archive = _unzip(resp.content)
    assert "project.json" in archive
    assert "README.md" in archive
    assert archive["files/readme.md"] == b"# Bench\n\nRoot file."
    assert archive["files/models/widget.stl"] == b"solid teapot\n"

    data = json.loads(archive["project.json"].decode("utf-8"))
    assert data["benchlog_export_version"] == 1
    assert data["slug"] == "bench"
    assert data["title"] == "Bench"
    assert data["status"] == "in_progress"
    assert data["is_public"] is True
    assert data["owner"]["username"] == "alice"
    assert data["tags"] == ["woodwork"]

    # Owner sees both updates (including private).
    update_titles = {u["title"] for u in data["updates"]}
    assert update_titles == {"Day 1", "Todo"}

    # Both files are in the structured file list.
    file_paths = {(f["path"], f["filename"]) for f in data["files"]}
    assert file_paths == {("", "readme.md"), ("models", "widget.stl")}
    assert all(f["checksum"] for f in data["files"])

    # Links are listed.
    assert [link["title"] for link in data["links"]] == ["Inspiration"]


# ---------- guest export ---------- #


async def test_guest_export_on_public_project_strips_private_updates(client, db):
    await _seed_rich_project(db, is_public=True)
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="public.md", content=b"public content", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )

    client.cookies.clear()
    resp = await client.get("/u/alice/bench/export")
    assert resp.status_code == 200
    archive = _unzip(resp.content)
    data = json.loads(archive["project.json"].decode("utf-8"))

    # Guest only sees the public update.
    update_titles = {u["title"] for u in data["updates"]}
    assert update_titles == {"Day 1"}
    assert "Todo" not in update_titles
    # But files are still fully included (files inherit project visibility,
    # no per-file private flag).
    assert "files/public.md" in archive


async def test_guest_export_on_private_project_404s(client, db):
    await _seed_rich_project(db, is_public=False)
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="secret.md", content=b"secret", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    client.cookies.clear()
    resp = await client.get("/u/alice/bench/export")
    assert resp.status_code == 404


# ---------- empty project ---------- #


async def test_export_works_on_empty_project(client, db):
    """A project with no files / updates / links should still export —
    just project.json + README with the basics."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(
        user_id=user.id, title="Empty", slug="empty",
        status=ProjectStatus.idea, is_public=True,
    ))
    await db.commit()
    await login(client, "alice")

    resp = await client.get("/u/alice/empty/export")
    assert resp.status_code == 200
    archive = _unzip(resp.content)
    assert set(archive.keys()) == {"project.json", "README.md"}
    data = json.loads(archive["project.json"].decode("utf-8"))
    assert data["updates"] == []
    assert data["links"] == []
    assert data["files"] == []


# ---------- README + updates.md contents ---------- #


async def test_readme_includes_title_description_links(client, db):
    await _seed_rich_project(db)
    await login(client, "alice")

    resp = await client.get("/u/alice/bench/export")
    archive = _unzip(resp.content)
    readme = archive["README.md"].decode("utf-8")
    assert "# Bench" in readme
    assert "*by alice*" in readme
    assert "A sturdy workbench" in readme
    # README references updates.md rather than inlining them.
    assert "## Updates" in readme
    assert "[`updates.md`](updates.md)" in readme
    # Update bodies don't appear in README — they're in updates.md.
    assert "Glued the tenons." not in readme
    assert "Still need to sand." not in readme
    assert "## Links" in readme
    assert "[Inspiration](https://example.com/bench)" in readme


async def test_updates_md_contains_every_update_for_owner(client, db):
    await _seed_rich_project(db)
    await login(client, "alice")

    resp = await client.get("/u/alice/bench/export")
    archive = _unzip(resp.content)
    assert "updates.md" in archive
    updates_md = archive["updates.md"].decode("utf-8")
    assert "# Updates — Bench" in updates_md
    assert "Day 1" in updates_md
    assert "Glued the tenons." in updates_md
    # Private updates are flagged, not hidden, on an owner export.
    assert "Todo" in updates_md
    assert "Still need to sand." in updates_md
    assert "_(private)_" in updates_md


async def test_updates_md_for_guest_strips_private(client, db):
    await _seed_rich_project(db, is_public=True)
    client.cookies.clear()

    resp = await client.get("/u/alice/bench/export")
    archive = _unzip(resp.content)
    updates_md = archive["updates.md"].decode("utf-8")
    # Only the public update renders for a guest.
    assert "Glued the tenons." in updates_md
    assert "Still need to sand." not in updates_md
    assert "_(private)_" not in updates_md


async def test_updates_md_is_omitted_when_no_updates(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(
        user_id=user.id, title="Silent", slug="silent",
        status=ProjectStatus.idea, is_public=True,
    ))
    await db.commit()
    await login(client, "alice")

    resp = await client.get("/u/alice/silent/export")
    archive = _unzip(resp.content)
    assert "updates.md" not in archive


# ---------- README: cover + gallery annotations ---------- #


async def test_readme_tags_cover_and_gallery_files(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id, title="Bench", slug="bench",
        status=ProjectStatus.in_progress, is_public=True,
    )
    db.add(project)
    await db.commit()
    await login(client, "alice")
    # Upload an image (auto-lands in gallery).
    import io as _io
    from PIL import Image as _Image
    img = _Image.new("RGB", (16, 16), color=(120, 120, 120))
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    await _upload(
        client, "/u/alice/bench/files",
        filename="hero.png", content=png_bytes, mime="image/png",
        csrf_path="/u/alice/bench",
    )
    # A second image we'll hide from the gallery.
    await _upload(
        client, "/u/alice/bench/files",
        filename="backup.png", content=png_bytes, mime="image/png",
        csrf_path="/u/alice/bench",
    )
    # A non-image.
    await _upload(
        client, "/u/alice/bench/files",
        filename="notes.md", content=b"plain text", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    from benchlog.models import ProjectFile
    from sqlalchemy import select as _select
    files = (await db.execute(_select(ProjectFile))).scalars().all()
    hero = next(f for f in files if f.filename == "hero.png")
    backup = next(f for f in files if f.filename == "backup.png")

    # Set hero as cover + hide backup from gallery.
    cover_token = await csrf_token(client, f"/u/alice/bench/files/{hero.id}")
    await client.post(
        f"/u/alice/bench/files/{hero.id}/cover", data={"_csrf": cover_token}
    )
    hide_token = await csrf_token(client, f"/u/alice/bench/files/{backup.id}")
    await client.post(
        f"/u/alice/bench/files/{backup.id}/gallery-visibility",
        data={"_csrf": hide_token},
    )

    resp = await client.get("/u/alice/bench/export")
    archive = _unzip(resp.content)
    readme = archive["README.md"].decode("utf-8")
    # Header cover pointer is a clickable relative markdown link.
    assert "**Cover image:** [files/hero.png](files/hero.png)" in readme
    # Summary line mentions counts — 1 in gallery (backup is hidden), 1 cover.
    assert "3 files total" in readme
    assert "1 in gallery" in readme
    assert "1 cover image" in readme
    # Each file bullet is a clickable markdown link to its relative path.
    def _file_line(name: str) -> str:
        prefix = f"- [{name}](files/"
        return next(ln for ln in readme.splitlines() if ln.startswith(prefix))

    hero_line = _file_line("hero.png")
    assert "(files/hero.png)" in hero_line
    assert "cover" in hero_line
    assert "gallery" in hero_line
    backup_line = _file_line("backup.png")
    assert "cover" not in backup_line
    assert "gallery" not in backup_line
    notes_line = _file_line("notes.md")
    assert "cover" not in notes_line
    assert "gallery" not in notes_line


# ---------- header button ---------- #


async def test_readme_groups_files_by_folder_with_clickable_links(client, db):
    """Nested folders render as `### folder/` subsections. Root files
    go directly under the summary. Each entry is a markdown link whose
    href matches its relative path inside the zip."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(
        user_id=user.id, title="Tree", slug="tree",
        status=ProjectStatus.idea, is_public=True,
    ))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/tree/files",
        filename="root.md", content=b"x", mime="text/markdown",
        csrf_path="/u/alice/tree",
    )
    await _upload(
        client, "/u/alice/tree/files",
        filename="a.md", content=b"x", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/tree",
    )
    await _upload(
        client, "/u/alice/tree/files",
        filename="b.md", content=b"x", mime="text/markdown",
        extra_form={"path": "models/widgets"},
        csrf_path="/u/alice/tree",
    )

    resp = await client.get("/u/alice/tree/export")
    archive = _unzip(resp.content)
    readme = archive["README.md"].decode("utf-8")

    # Folder subsections exist and are in stable order (root first → others
    # alphabetical).
    assert "### models/" in readme
    assert "### models/widgets/" in readme
    models_idx = readme.index("### models/")
    widgets_idx = readme.index("### models/widgets/")
    assert models_idx < widgets_idx
    # Root file sits between the summary and the first folder heading.
    root_idx = readme.index("[root.md](files/root.md)")
    assert root_idx < models_idx
    # Each bullet is a relative markdown link so viewers can click through.
    assert "[a.md](files/models/a.md)" in readme
    assert "[b.md](files/models/widgets/b.md)" in readme


async def test_project_detail_renders_export_link_for_any_viewer(client, db):
    await _seed_rich_project(db, is_public=True)
    await login(client, "alice")
    resp = await client.get("/u/alice/bench")
    assert 'href="/u/alice/bench/export"' in resp.text

    client.cookies.clear()
    resp = await client.get("/u/alice/bench")
    assert 'href="/u/alice/bench/export"' in resp.text
