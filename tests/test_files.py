"""Tests for project files — upload, version, browse, download, visibility."""

import io
import shutil
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from benchlog.config import settings
from benchlog.files import (
    code_language,
    highlight_code,
    normalize_virtual_path,
    preview_kind,
    safe_filename,
)
from benchlog.markdown import rewrite_project_file_links
from benchlog.models import (
    FileVersion,
    Project,
    ProjectFile,
    ProjectStatus,
    ProjectUpdate,
)
from benchlog.storage import get_storage
from tests.conftest import csrf_token, login, make_user


# ---------- helpers ---------- #


@pytest.fixture(autouse=True)
def _isolate_storage(tmp_path, monkeypatch):
    """Each test gets its own temp storage root so blobs don't leak."""
    monkeypatch.setattr(settings, "storage_local_path", str(tmp_path / "files"))
    get_storage.cache_clear()
    yield
    get_storage.cache_clear()
    shutil.rmtree(tmp_path / "files", ignore_errors=True)


async def _upload(
    client,
    url: str,
    *,
    filename: str,
    content: bytes,
    mime: str = "application/octet-stream",
    extra_form: dict | None = None,
    csrf_path: str = "/projects",
):
    token = await csrf_token(client, csrf_path)
    data = {"_csrf": token, **(extra_form or {})}
    files = {"upload": (filename, content, mime)}
    return await client.post(url, data=data, files=files)


def _png_bytes(width: int = 32, height: int = 24) -> bytes:
    img = Image.new("RGB", (width, height), color=(180, 80, 60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_with_rotation_exif(width: int = 64, height: int = 100) -> bytes:
    """JPEG whose EXIF says orientation=6 (rotate 90° CW).

    PIL encodes orientation=6 when the source image is taller than wide
    but encoded as a wide image that should be displayed rotated. We
    simulate: encode a width×height image with orientation=6 so a naive
    thumbnail would come out sideways.
    """
    from PIL import Image as _Image
    img = _Image.new("RGB", (width, height), color=(60, 80, 180))
    buf = io.BytesIO()
    # Pillow 10+: save with exif bytes directly.
    exif = img.getexif()
    exif[0x0112] = 6  # Orientation tag
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


# ---------- path + filename normalization ---------- #


def test_normalize_virtual_path_strips_slashes_and_collapses():
    assert normalize_virtual_path("") == ""
    assert normalize_virtual_path(None) == ""
    assert normalize_virtual_path("/") == ""
    assert normalize_virtual_path("/models/") == "models"
    assert normalize_virtual_path("models//widgets") == "models/widgets"
    assert normalize_virtual_path("a\\b") == "a/b"


def test_normalize_virtual_path_rejects_dot_segments():
    with pytest.raises(ValueError):
        normalize_virtual_path("models/../etc")
    with pytest.raises(ValueError):
        normalize_virtual_path("./models")


def test_safe_filename_strips_path_components():
    assert safe_filename("widget.stl") == "widget.stl"
    assert safe_filename("models/widget.stl") == "widget.stl"
    assert safe_filename("..\\..\\evil.txt") == "evil.txt"
    with pytest.raises(ValueError):
        safe_filename("")
    with pytest.raises(ValueError):
        safe_filename("///")


def test_safe_filename_rejects_windows_unsafe_characters():
    """Chars forbidden on NTFS round-trip badly inside a downloaded zip."""
    for bad in ("<", ">", ":", '"', "|", "?", "*"):
        with pytest.raises(ValueError):
            safe_filename(f"file{bad}name.txt")


def test_safe_filename_rejects_trailing_period():
    with pytest.raises(ValueError):
        safe_filename("notes.")


def test_safe_filename_strips_trailing_space():
    # Trailing whitespace is normalized away rather than rejected — the
    # cleaned form is still safe to write to disk on every OS.
    assert safe_filename("notes.txt ") == "notes.txt"


def test_safe_filename_rejects_windows_reserved_names():
    for reserved in ("CON", "con", "PRN.txt", "COM1", "lpt9.log"):
        with pytest.raises(ValueError):
            safe_filename(reserved)


def test_safe_filename_accepts_leading_dot_dotfiles():
    """Dotfiles are legitimate, not traversal — keep them intact."""
    assert safe_filename(".gitignore") == ".gitignore"
    assert safe_filename(".env.production") == ".env.production"


def test_safe_filename_rejects_control_characters():
    with pytest.raises(ValueError):
        safe_filename("bad\x00name")
    with pytest.raises(ValueError):
        safe_filename("bad\tname")


def test_normalize_virtual_path_rejects_unsafe_segment():
    with pytest.raises(ValueError):
        normalize_virtual_path("models/bad:name")
    with pytest.raises(ValueError):
        normalize_virtual_path("CON/inner")
    # A trailing space inside a segment (between the segment and the next
    # `/`) is unsafe — outer whitespace is stripped, inner segment-trailing
    # whitespace gets caught by the per-segment validation.
    with pytest.raises(ValueError):
        normalize_virtual_path("trailing /folder")


# ---------- multipart CSRF ---------- #


async def test_multipart_upload_with_valid_csrf_in_form_passes(client, db):
    """Proves the CSRFMiddleware now accepts multipart bodies (was 415)."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()

    await login(client, "alice")
    resp = await _upload(
        client,
        "/u/alice/bench/files",
        filename="notes.txt",
        content=b"hello",
        mime="text/plain",
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 302


async def test_multipart_upload_rejects_missing_csrf(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()

    await login(client, "alice")
    # No CSRF token in form OR header.
    files = {"upload": ("notes.txt", b"hello", "text/plain")}
    resp = await client.post("/u/alice/bench/files", files=files)
    assert resp.status_code == 403
    assert "CSRF" in resp.text


async def test_multipart_upload_csrf_via_header_passes(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()

    await login(client, "alice")
    token = await csrf_token(client, "/u/alice/bench")
    files = {"upload": ("notes.txt", b"hello", "text/plain")}
    resp = await client.post(
        "/u/alice/bench/files", files=files, headers={"X-CSRF-Token": token}
    )
    assert resp.status_code == 302


# ---------- upload happy path ---------- #


async def test_upload_creates_file_with_version_one(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea)
    db.add(project)
    await db.commit()

    await login(client, "alice")
    resp = await _upload(
        client,
        "/u/alice/bench/files",
        filename="model.stl",
        content=b"solid teapot\n",
        mime="model/stl",
        extra_form={"path": "models", "description": "Initial model"},
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 302

    file = (
        await db.execute(
            select(ProjectFile)
            .options(selectinload(ProjectFile.versions), selectinload(ProjectFile.current_version))
        )
    ).scalar_one()
    assert file.filename == "model.stl"
    assert file.path == "models"
    assert file.description == "Initial model"
    assert len(file.versions) == 1
    v = file.versions[0]
    assert v.version_number == 1
    assert v.size_bytes == len(b"solid teapot\n")
    assert v.mime_type == "model/stl"
    assert len(v.checksum) == 64  # sha256 hex
    assert file.current_version_id == v.id

    # Blob exists on disk at the expected path.
    storage = get_storage()
    assert storage.full_path(v.storage_path).exists()


async def test_upload_image_generates_thumbnail(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()

    await login(client, "alice")
    resp = await _upload(
        client,
        "/u/alice/bench/files",
        filename="photo.png",
        content=_png_bytes(800, 600),
        mime="image/png",
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 302

    v = (await db.execute(select(FileVersion))).scalar_one()
    assert v.mime_type == "image/png"
    assert v.width == 800
    assert v.height == 600
    assert v.thumbnail_path is not None

    storage = get_storage()
    assert storage.full_path(v.thumbnail_path).exists()


# ---------- version bump ---------- #


async def test_repeat_upload_to_same_path_creates_new_version(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()

    await login(client, "alice")
    await _upload(
        client,
        "/u/alice/bench/files",
        filename="model.stl",
        content=b"v1",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client,
        "/u/alice/bench/files",
        filename="model.stl",
        content=b"v2-updated",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )

    files = (await db.execute(select(ProjectFile))).scalars().all()
    assert len(files) == 1  # same row, not a duplicate
    file = files[0]

    versions = (
        await db.execute(
            select(FileVersion).where(FileVersion.file_id == file.id).order_by(FileVersion.version_number)
        )
    ).scalars().all()
    assert [v.version_number for v in versions] == [1, 2]
    assert versions[0].size_bytes == 2
    assert versions[1].size_bytes == 10
    await db.refresh(file)
    assert file.current_version_id == versions[1].id


async def test_explicit_version_endpoint_creates_new_version_with_changelog(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()

    await login(client, "alice")
    await _upload(
        client,
        "/u/alice/bench/files",
        filename="brief.md",
        content=b"draft",
        mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    resp = await _upload(
        client,
        f"/u/alice/bench/files/{file.id}/version",
        filename="brief.md",
        content=b"v2 with edits",
        mime="text/markdown",
        extra_form={"changelog": "fixed typos"},
        csrf_path=f"/u/alice/bench/files/{file.id}",
    )
    assert resp.status_code == 302

    versions = (
        await db.execute(
            select(FileVersion).where(FileVersion.file_id == file.id).order_by(FileVersion.version_number)
        )
    ).scalars().all()
    assert [v.version_number for v in versions] == [1, 2]
    assert versions[1].changelog == "fixed typos"


# ---------- download ---------- #


async def test_download_returns_current_version_by_default(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()

    await login(client, "alice")
    await _upload(
        client,
        "/u/alice/bench/files",
        filename="brief.md",
        content=b"first",
        mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    await _upload(
        client,
        "/u/alice/bench/files",
        filename="brief.md",
        content=b"second",
        mime="text/markdown",
        csrf_path="/u/alice/bench",
    )

    resp = await client.get(f"/u/alice/bench/files/{file.id}/download")
    assert resp.status_code == 200
    assert resp.content == b"second"
    assert "brief.md" in resp.headers["content-disposition"]


async def test_download_explicit_v_returns_that_version(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()

    await login(client, "alice")
    await _upload(
        client,
        "/u/alice/bench/files",
        filename="brief.md",
        content=b"first",
        mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    await _upload(
        client,
        "/u/alice/bench/files",
        filename="brief.md",
        content=b"second",
        mime="text/markdown",
        csrf_path="/u/alice/bench",
    )

    resp = await client.get(f"/u/alice/bench/files/{file.id}/download?v=1")
    assert resp.status_code == 200
    assert resp.content == b"first"


async def test_download_unknown_version_returns_404(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()

    await login(client, "alice")
    await _upload(
        client,
        "/u/alice/bench/files",
        filename="brief.md",
        content=b"first",
        mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    resp = await client.get(f"/u/alice/bench/files/{file.id}/download?v=99")
    assert resp.status_code == 404


# ---------- visibility ---------- #


async def test_guest_can_view_files_on_public_project(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id, title="Bench", slug="bench",
        status=ProjectStatus.in_progress, is_public=True,
    )
    db.add(project)
    await db.commit()
    await login(client, "alice")
    await _upload(
        client,
        "/u/alice/bench/files",
        filename="readme.md",
        content=b"public",
        mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    # Guest has no session — drop it.
    client.cookies.clear()

    resp = await client.get("/u/alice/bench/files")
    assert resp.status_code == 200
    assert "readme.md" in resp.text

    resp = await client.get(f"/u/alice/bench/files/{file.id}")
    assert resp.status_code == 200

    resp = await client.get(f"/u/alice/bench/files/{file.id}/download")
    assert resp.status_code == 200
    assert resp.content == b"public"


async def test_guest_cannot_view_files_on_private_project(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea)
    db.add(project)
    await db.commit()

    await login(client, "alice")
    await _upload(
        client,
        "/u/alice/bench/files",
        filename="secret.md",
        content=b"private",
        mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    client.cookies.clear()

    assert (await client.get("/u/alice/bench/files")).status_code == 404
    assert (await client.get(f"/u/alice/bench/files/{file.id}")).status_code == 404
    assert (await client.get(f"/u/alice/bench/files/{file.id}/download")).status_code == 404


async def test_non_owner_cannot_upload_or_delete_or_edit(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    project = Project(
        user_id=alice.id, title="Bench", slug="bench",
        status=ProjectStatus.in_progress, is_public=True,
    )
    db.add(project)
    await db.commit()

    # Alice uploads a file first.
    await login(client, "alice")
    await _upload(
        client,
        "/u/alice/bench/files",
        filename="readme.md",
        content=b"alice",
        mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    # Bob takes over the session.
    await login(client, "bob")

    # Bob tries to upload to alice's project — 404 (URL username mismatch).
    resp = await _upload(
        client,
        "/u/alice/bench/files",
        filename="bobtried.md",
        content=b"x",
        mime="text/markdown",
        csrf_path="/projects",
    )
    assert resp.status_code == 404

    # Bob tries to delete alice's file.
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/delete", data={"_csrf": token}
    )
    assert resp.status_code == 404

    # Bob tries to load the edit form.
    resp = await client.get(f"/u/alice/bench/files/{file.id}/edit")
    assert resp.status_code == 404


# ---------- thumbnail + cover image ---------- #


async def test_thumbnail_endpoint_returns_webp_for_image(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client,
        "/u/alice/bench/files",
        filename="photo.png",
        content=_png_bytes(),
        mime="image/png",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    resp = await client.get(f"/u/alice/bench/files/{file.id}/thumb")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"


async def test_thumbnail_404s_for_non_image(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client,
        "/u/alice/bench/files",
        filename="notes.txt",
        content=b"hello",
        mime="text/plain",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    resp = await client.get(f"/u/alice/bench/files/{file.id}/thumb")
    assert resp.status_code == 404


async def test_owner_sets_image_as_cover(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client,
        "/u/alice/bench/files",
        filename="hero.png",
        content=_png_bytes(),
        mime="image/png",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/cover", data={"_csrf": token}
    )
    assert resp.status_code == 302

    project = (await db.execute(select(Project))).scalar_one()
    assert project.cover_file_id == file.id


async def test_setting_non_image_as_cover_400s(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client,
        "/u/alice/bench/files",
        filename="notes.txt",
        content=b"hello",
        mime="text/plain",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/cover", data={"_csrf": token}
    )
    assert resp.status_code == 400


# ---------- edit + delete ---------- #


async def test_owner_can_rename_and_move_file(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client,
        "/u/alice/bench/files",
        filename="draft.md",
        content=b"x",
        mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}/edit")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}",
        data={
            "_csrf": token,
            "filename": "final.md",
            "path": "drafts",
            "description": "ready to ship",
        },
    )
    assert resp.status_code == 302

    await db.refresh(file)
    assert file.filename == "final.md"
    assert file.path == "drafts"
    assert file.description == "ready to ship"


async def test_rename_blocks_collision(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.md", content=b"a", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="b.md", content=b"b", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    files = (await db.execute(select(ProjectFile))).scalars().all()
    a = next(f for f in files if f.filename == "a.md")

    token = await csrf_token(client, f"/u/alice/bench/files/{a.id}/edit")
    resp = await client.post(
        f"/u/alice/bench/files/{a.id}",
        data={"_csrf": token, "filename": "b.md", "path": "", "description": ""},
    )
    # Collision now returns 409 (Conflict) instead of a generic 400 so the
    # modal can show a pointed error when fetched with Accept: json.
    assert resp.status_code == 409
    assert "already exists" in resp.text


async def test_owner_deletes_file_and_blob_is_removed(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="dispose.md", content=b"bye", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    version = (await db.execute(select(FileVersion))).scalar_one()
    blob_path = Path(get_storage().full_path(version.storage_path))
    assert blob_path.exists()

    token = await csrf_token(client, "/u/alice/bench/files")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/delete", data={"_csrf": token}
    )
    assert resp.status_code == 302

    remaining = (await db.execute(select(ProjectFile))).scalars().all()
    assert remaining == []
    assert not blob_path.exists()


async def test_deleting_project_cascades_to_files(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="x.md", content=b"x", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )

    token = await csrf_token(client, "/projects")
    await client.post("/u/alice/bench/delete", data={"_csrf": token})

    files = (await db.execute(select(ProjectFile))).scalars().all()
    versions = (await db.execute(select(FileVersion))).scalars().all()
    assert files == []
    assert versions == []


# ---------- tab integration ---------- #


async def test_files_tab_appears_in_project_nav(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    resp = await client.get("/u/alice/bench")
    assert 'href="/u/alice/bench/files"' in resp.text
    resp = await client.get("/u/alice/bench/files")
    assert resp.status_code == 200
    anchor_start = resp.text.index('href="/u/alice/bench/files"')
    assert 'aria-current="page"' in resp.text[anchor_start:anchor_start + 300]


# ---------- EXIF orientation ---------- #


async def test_thumbnail_respects_exif_orientation(client, db):
    """Upload a 64x100 JPEG with EXIF orientation=6 (rotate 90° CW).
    After exif_transpose the effective image is 100x64, so the stored
    width/height should reflect the rotated dimensions."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="vertical.jpg",
        content=_jpeg_with_rotation_exif(64, 100),
        mime="image/jpeg",
        csrf_path="/u/alice/bench",
    )
    v = (await db.execute(select(FileVersion))).scalar_one()
    # Orientation=6 means the encoded 64x100 should display as 100x64.
    assert (v.width, v.height) == (100, 64)

    # Thumbnail dimensions should also reflect the rotated orientation —
    # whichever side is longer after rotation stays the longer side.
    storage = get_storage()
    thumb_bytes = await storage.read(v.thumbnail_path)
    with Image.open(io.BytesIO(thumb_bytes)) as thumb:
        assert thumb.width > thumb.height


# ---------- preview_kind ---------- #


def test_preview_kind_dispatches_by_mime_and_extension():
    assert preview_kind("image/png", "a.png") == "image"
    assert preview_kind("video/mp4", "a.mp4") == "video"
    assert preview_kind("audio/mpeg", "a.mp3") == "audio"
    assert preview_kind("application/pdf", "a.pdf") == "pdf"
    # Markdown has a Pygments lexer, so it renders via the "code" path now.
    assert preview_kind("text/markdown", "a.md") == "code"
    # Extension fallback when server sends octet-stream.
    assert preview_kind("application/octet-stream", "README.md") == "code"
    # Textual but no lexer -> plain "text" fallback.
    assert preview_kind("text/plain", "server.log") == "text"
    assert preview_kind("application/octet-stream", "model.stl") == "none"


async def test_detail_renders_inline_video_for_video_upload(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="clip.mp4", content=b"\x00\x00\x00 ftypmp42fake",
        mime="video/mp4", csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    resp = await client.get(f"/u/alice/bench/files/{file.id}")
    assert resp.status_code == 200
    assert "<video" in resp.text
    assert f"/u/alice/bench/files/{file.id}/download" in resp.text


async def test_detail_renders_text_preview_for_markdown(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="README.md", content=b"# preview-marker-alpha\nhello",
        mime="text/markdown", csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    resp = await client.get(f"/u/alice/bench/files/{file.id}")
    assert resp.status_code == 200
    assert "<pre" in resp.text
    assert "preview-marker-alpha" in resp.text


# ---------- Pygments code highlighting ---------- #


def test_code_language_resolves_known_extensions():
    assert code_language("script.py") == "python"
    assert code_language("app.js") == "javascript"
    assert code_language("main.rs") == "rust"
    assert code_language("part.scad") == "openscad"
    # Extensions Pygments doesn't know about -> None.
    assert code_language("notes.weird-ext") is None
    # Plain text maps to TextLexer which we treat as "no lexer".
    assert code_language("log.txt") is None


def test_highlight_code_emits_line_numbers_and_token_spans():
    html = highlight_code("def hello():\n    return 1\n", "python")
    # linenos="table" wraps the whole block in a <table class="highlighttable">.
    assert "highlighttable" in html
    # Line numbers are rendered as anchored spans inside the linenos cell.
    assert 'class="linenos"' in html
    # `def` is a Python keyword -> <span class="k">def</span>.
    assert '<span class="k">def</span>' in html
    # Wrapper div with the cssclass we configured.
    assert '<div class="highlight">' in html


async def test_python_code_file_renders_with_pygments_highlighting(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="script.py",
        content=b"def hello():\n    return 1\n",
        mime="text/x-python",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    resp = await client.get(f"/u/alice/bench/files/{file.id}")
    assert resp.status_code == 200
    body = resp.text
    # Pygments wrapper + keyword token proves highlighting ran.
    assert '<div class="highlight">' in body
    assert '<span class="k">def</span>' in body
    # linenos="table" renders a dedicated line-numbers cell.
    assert 'class="linenos"' in body
    # Language label is surfaced to the reader.
    assert "python" in body


async def test_unknown_extension_falls_back_to_plain_text(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    # A `.log` is textual but has no Pygments lexer -> plain <pre><code>.
    await _upload(
        client, "/u/alice/bench/files",
        filename="server.log",
        content=b"plain-text-marker-zeta\n",
        mime="text/plain",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    resp = await client.get(f"/u/alice/bench/files/{file.id}")
    assert resp.status_code == 200
    body = resp.text
    assert "plain-text-marker-zeta" in body
    # Should NOT have been routed through Pygments.
    assert '<div class="highlight">' not in body
    assert "<pre" in body


async def test_javascript_code_file_uses_pygments(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="app.js",
        content=b"const x = 1;\n",
        mime="application/javascript",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    resp = await client.get(f"/u/alice/bench/files/{file.id}")
    assert resp.status_code == 200
    body = resp.text
    assert '<div class="highlight">' in body
    # `const` is a JS keyword/declaration token.
    assert 'class="kd"' in body or 'class="k"' in body
    assert "javascript" in body


async def test_code_preview_truncates_at_size_limit(client, db):
    """Files over 256 KB are truncated and the template shows the warning."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    # Generate > 256 KB of valid-ish Python so the lexer doesn't choke.
    lines = [f"x_{i} = {i}\n" for i in range(40000)]
    payload = "".join(lines).encode("utf-8")
    assert len(payload) > 256 * 1024
    await _upload(
        client, "/u/alice/bench/files",
        filename="big.py", content=payload,
        mime="text/x-python", csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    resp = await client.get(f"/u/alice/bench/files/{file.id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Preview truncated" in body
    # Response size is bounded: Pygments expands each source byte into
    # many HTML bytes (spans, anchors, table rows), but we cap the source
    # at 256 KB, so the HTML can't balloon past a few MB — the full
    # ~700 KB payload highlighted without the cap would be much larger.
    assert len(body) < 6 * 1024 * 1024


# ---------- file tree ---------- #


async def test_files_tab_renders_all_files_in_a_table(client, db):
    """The tree renders every file on a single page — nested folders
    produce their own <tr> with a toggle button and descendants as
    sibling rows carrying a `data-parent-path` pointing back at the folder."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="root-marker-alpha.md", content=b"x", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="shallow-marker-bravo.md", content=b"x", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="deep-marker-charlie.md", content=b"x", mime="text/markdown",
        extra_form={"path": "models/widgets"},
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/files")
    assert resp.status_code == 200
    body = resp.text
    # Every file is rendered, at any depth.
    assert "root-marker-alpha.md" in body
    assert "shallow-marker-bravo.md" in body
    assert "deep-marker-charlie.md" in body
    # Table scaffolding + header row are present.
    assert 'class="file-tree' in body
    header_block = body[body.index("<thead"):body.index("</thead>")]
    assert ">Name<" in header_block
    assert ">Size<" in header_block
    # Nested file carries its parent-path so the JS collapse logic can hide it.
    assert 'data-parent-path="models/widgets"' in body


async def test_file_tree_default_sort_is_name_asc(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="zebra-marker.md", content=b"z", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="alpha-marker.md", content=b"a", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/files")
    body = resp.text
    # Alphabetical by default — alpha renders before zebra.
    assert body.index("alpha-marker.md") < body.index("zebra-marker.md")
    # Default active indicator is on Name asc.
    header_block = body[body.index("<thead"):body.index("</thead>")]
    assert 'aria-sort="ascending"' in header_block
    assert "chevron-up" in header_block


async def test_file_tree_sort_by_name_desc_reverses_order(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="alpha-marker.md", content=b"a", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="zebra-marker.md", content=b"z", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/files?sort=name&dir=desc")
    body = resp.text
    assert body.index("zebra-marker.md") < body.index("alpha-marker.md")
    header_block = body[body.index("<thead"):body.index("</thead>")]
    assert 'aria-sort="descending"' in header_block
    assert "chevron-down" in header_block


async def test_file_tree_sort_by_size_puts_largest_first_when_desc(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="tiny-alpha.bin", content=b"x" * 10, mime="application/octet-stream",
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="huge-bravo.bin", content=b"x" * 10_000, mime="application/octet-stream",
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/files?sort=size&dir=desc")
    body = resp.text
    assert body.index("huge-bravo.bin") < body.index("tiny-alpha.bin")


async def test_folders_and_files_sort_as_one_list_not_folders_first(client, db):
    """Folders don't pin above files — a folder named after a file should
    sort to its alphabetical position, Finder-style."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    # A root-level file whose name sorts BEFORE the folder "zebra-folder".
    await _upload(
        client, "/u/alice/bench/files",
        filename="alpha-file-marker.md", content=b"a", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    # A folder "zebra-folder" with a file inside.
    await _upload(
        client, "/u/alice/bench/files",
        filename="inner.md", content=b"i", mime="text/markdown",
        extra_form={"path": "zebra-folder"},
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/files?sort=name&dir=asc")
    body = resp.text
    # alpha-file-marker.md (a) renders before the zebra-folder row (z).
    assert body.index("alpha-file-marker.md") < body.index('data-folder-path="zebra-folder"')

    # Flipping direction swaps them — folder comes before the file now.
    resp = await client.get("/u/alice/bench/files?sort=name&dir=desc")
    body = resp.text
    assert body.index('data-folder-path="zebra-folder"') < body.index("alpha-file-marker.md")


async def test_file_and_folder_rows_carry_title_tooltip(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="this-is-a-long-filename-that-could-truncate.md",
        content=b"x", mime="text/markdown",
        extra_form={"path": "deep/nested"},
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/files")
    body = resp.text
    # File anchor carries title with full filename for hover tooltip.
    assert 'title="this-is-a-long-filename-that-could-truncate.md"' in body
    # Folder label carries title with its full path.
    assert 'title="deep/nested"' in body


async def test_file_tree_sort_applies_at_every_depth(client, db):
    """Sort is applied recursively — nested folders reorder too."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="nested-zebra.md", content=b"z", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="nested-alpha.md", content=b"a", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/files?sort=name&dir=desc")
    body = resp.text
    assert body.index("nested-zebra.md") < body.index("nested-alpha.md")


async def test_file_tree_invalid_sort_params_fall_back_to_defaults(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="x.md", content=b"x", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    # Garbage sort params shouldn't 500 or change the default order.
    resp = await client.get("/u/alice/bench/files?sort=haxx&dir=up")
    assert resp.status_code == 200
    # Active indicator stays on Name asc.
    header_block = resp.text[resp.text.index("<thead"):resp.text.index("</thead>")]
    assert 'aria-sort="ascending"' in header_block


async def test_folder_row_shows_total_size_of_descendants(client, db):
    """A folder's Size column aggregates every descendant file's size."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    # 2 KB directly under "models" + 3 KB in "models/widgets" = 5 KB total.
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.bin", content=b"x" * 2048, mime="application/octet-stream",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="b.bin", content=b"x" * 3072, mime="application/octet-stream",
        extra_form={"path": "models/widgets"},
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/files")
    body = resp.text
    # Find the "models" folder row and confirm its Size cell shows 5.0 KB.
    models_row_start = body.index('data-folder-path="models"')
    row_end = body.index("</tr>", models_row_start)
    models_row = body[models_row_start:row_end]
    assert "5.0 KB" in models_row
    # The nested widgets folder itself is 3 KB.
    widgets_row_start = body.index('data-folder-path="models/widgets"')
    widgets_row_end = body.index("</tr>", widgets_row_start)
    widgets_row = body[widgets_row_start:widgets_row_end]
    assert "3.0 KB" in widgets_row


async def test_folder_size_shows_recursive_file_count_in_parens(client, db):
    """Count only files, not folders. 'models' has 1 direct file + 1
    nested file = (2); 'models/widgets' has just the nested file = (1)."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.bin", content=b"x" * 1024, mime="application/octet-stream",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="b.bin", content=b"x" * 1024, mime="application/octet-stream",
        extra_form={"path": "models/widgets"},
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/files")
    body = resp.text
    models_row = body[body.index('data-folder-path="models"'):body.index("</tr>", body.index('data-folder-path="models"'))]
    widgets_row = body[body.index('data-folder-path="models/widgets"'):body.index("</tr>", body.index('data-folder-path="models/widgets"'))]
    assert "(2)" in models_row
    assert "(1)" in widgets_row


async def test_folder_modified_column_shows_newest_descendant_date(client, db):
    """The Modified column on folder rows should render the most recent
    descendant file's upload date — not a file count."""
    from datetime import datetime, timezone, timedelta

    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea)
    db.add(project)
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="old.bin", content=b"x", mime="application/octet-stream",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="new.bin", content=b"x", mime="application/octet-stream",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    # Hand-set timestamps so there's a guaranteed newest file.
    newest_ts = datetime(2030, 6, 15, tzinfo=timezone.utc)
    oldest_ts = newest_ts - timedelta(days=365)
    files = (await db.execute(select(ProjectFile))).scalars().all()
    files_by_name = {f.filename: f for f in files}
    for version in (await db.execute(select(FileVersion))).scalars().all():
        if version.file_id == files_by_name["old.bin"].id:
            version.uploaded_at = oldest_ts
        else:
            version.uploaded_at = newest_ts
    await db.commit()

    resp = await client.get("/u/alice/bench/files")
    body = resp.text
    models_row = body[body.index('data-folder-path="models"'):body.index("</tr>", body.index('data-folder-path="models"'))]
    assert "2030-06-15" in models_row
    # The old date should NOT appear in the folder's Modified cell.
    assert "2029-06" not in models_row


async def test_empty_folder_modified_shows_dash(client, db):
    """With no descendant files, max_modified is None — show an em-dash
    instead of a bogus date."""
    # Creating an "empty folder" via a subfolder path where the only file
    # lives deeper than this row.
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    # A file deeply nested — the parent "outer" folder has no direct files
    # but total_file_count rolls up from the descendant.
    await _upload(
        client, "/u/alice/bench/files",
        filename="x.md", content=b"x", mime="text/markdown",
        extra_form={"path": "outer/inner"},
        csrf_path="/u/alice/bench",
    )
    resp = await client.get("/u/alice/bench/files")
    body = resp.text
    # The "outer" folder shows the file count (1) — it rolls up.
    outer_row = body[body.index('data-folder-path="outer"'):body.index("</tr>", body.index('data-folder-path="outer"'))]
    assert "(1)" in outer_row


async def test_files_tree_folder_labels_and_toggle_present(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="x.md", content=b"x", mime="text/markdown",
        extra_form={"path": "models/widgets"},
        csrf_path="/u/alice/bench",
    )
    resp = await client.get("/u/alice/bench/files")
    body = resp.text
    # Folder rows carry their full path as data-folder-path.
    assert 'data-folder-path="models"' in body
    assert 'data-folder-path="models/widgets"' in body
    # Folder names appear as the clickable label.
    assert ">models<" in body
    assert ">widgets<" in body
    # Each folder has a toggle button with aria state.
    assert 'class="file-tree-toggle"' in body
    assert 'aria-expanded="true"' in body


# ---------- gallery ---------- #


async def test_gallery_lists_only_image_files(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="hero.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="notes.txt", content=b"hi", mime="text/plain",
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/gallery")
    assert resp.status_code == 200
    assert "hero.png" in resp.text
    assert "notes.txt" not in resp.text


async def test_gallery_empty_state_for_no_images(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    resp = await client.get("/u/alice/bench/gallery")
    assert resp.status_code == 200
    assert "No images in the gallery yet" in resp.text


async def test_gallery_visible_to_guest_on_public_project(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(
        user_id=user.id, title="Bench", slug="bench",
        status=ProjectStatus.in_progress, is_public=True,
    ))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="hero.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    client.cookies.clear()
    resp = await client.get("/u/alice/bench/gallery")
    assert resp.status_code == 200
    assert "hero.png" in resp.text


async def test_gallery_404s_for_guest_on_private_project(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="secret.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    client.cookies.clear()
    resp = await client.get("/u/alice/bench/gallery")
    assert resp.status_code == 404


# ---------- show_in_gallery toggle ---------- #


async def test_hidden_image_is_excluded_from_guest_gallery(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(
        user_id=user.id, title="Bench", slug="bench",
        status=ProjectStatus.in_progress, is_public=True,
    ))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="featured-alpha.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="testshot-bravo.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    files = (await db.execute(select(ProjectFile))).scalars().all()
    hidden = next(f for f in files if f.filename == "testshot-bravo.png")

    token = await csrf_token(client, f"/u/alice/bench/files/{hidden.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{hidden.id}/gallery-visibility",
        data={"_csrf": token},
    )
    assert resp.status_code == 302
    await db.refresh(hidden)
    assert hidden.show_in_gallery is False

    # Owner still sees both (marked with a hidden badge).
    resp = await client.get("/u/alice/bench/gallery")
    assert "featured-alpha.png" in resp.text
    assert "testshot-bravo.png" in resp.text

    # Guest sees only the featured image.
    client.cookies.clear()
    resp = await client.get("/u/alice/bench/gallery")
    assert "featured-alpha.png" in resp.text
    assert "testshot-bravo.png" not in resp.text


async def test_hiding_the_cover_image_clears_the_cover(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="hero.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    # Set as cover.
    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    await client.post(f"/u/alice/bench/files/{file.id}/cover", data={"_csrf": token})
    project = (await db.execute(select(Project))).scalar_one()
    assert project.cover_file_id == file.id

    # Hide from gallery — should also clear the cover, since a hidden cover
    # would mean the card shows an image that's not in the gallery.
    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    await client.post(
        f"/u/alice/bench/files/{file.id}/gallery-visibility", data={"_csrf": token}
    )
    await db.refresh(project)
    assert project.cover_file_id is None


async def test_hidden_images_sit_under_an_accordion_for_owner(client, db):
    """Owners see visible images in the top grid and hidden images tucked
    inside a <details> accordion — so the default view matches what
    guests see."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="featured-alpha.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="hidden-bravo.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    hidden = next(
        f for f in (await db.execute(select(ProjectFile))).scalars().all()
        if f.filename == "hidden-bravo.png"
    )
    token = await csrf_token(client, f"/u/alice/bench/files/{hidden.id}")
    await client.post(
        f"/u/alice/bench/files/{hidden.id}/gallery-visibility",
        data={"_csrf": token},
    )

    resp = await client.get("/u/alice/bench/gallery")
    body = resp.text
    assert "featured-alpha.png" in body
    assert "hidden-bravo.png" in body
    # Accordion is labelled with the Hidden summary. The hidden image
    # renders AFTER that summary (inside the <details>), and the featured
    # image renders BEFORE it (in the top grid).
    accordion_marker = "Hidden from gallery ("
    assert accordion_marker in body
    accordion_start = body.index(accordion_marker)
    assert body.index("featured-alpha.png") < accordion_start
    assert body.index("hidden-bravo.png") > accordion_start


async def test_gallery_toggle_requires_owner(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    db.add(Project(
        user_id=alice.id, title="Bench", slug="bench",
        status=ProjectStatus.in_progress, is_public=True,
    ))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="hero.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/gallery-visibility", data={"_csrf": token}
    )
    assert resp.status_code == 404


# ---------- gallery lightbox ---------- #


async def test_gallery_page_includes_lightbox_data_block(client, db):
    """The gallery page emits a JSON <script> block listing every visible
    image — gallery-lightbox.js parses it on load."""
    import json
    import re

    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="alpha.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="bravo.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    files = (await db.execute(select(ProjectFile))).scalars().all()
    expected_ids = {str(f.id) for f in files}

    resp = await client.get("/u/alice/bench/gallery")
    assert resp.status_code == 200
    assert 'id="gallery-lightbox-data"' in resp.text

    match = re.search(
        r'<script type="application/json" id="gallery-lightbox-data">(.*?)</script>',
        resp.text,
        re.DOTALL,
    )
    assert match, "lightbox data block not found"
    data = json.loads(match.group(1))
    assert isinstance(data, list)
    assert len(data) == 2
    assert {entry["id"] for entry in data} == expected_ids
    for entry in data:
        assert entry["full_url"].endswith(f"/files/{entry['id']}/download")
        assert entry["thumb_url"].endswith(f"/files/{entry['id']}/thumb")
        assert "filename" in entry
        assert "description" in entry


async def test_gallery_page_includes_lightbox_dialog_markup(client, db):
    """The lightbox <dialog> markup ships with the gallery page so the JS
    has something to attach to."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="hero.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/gallery")
    assert resp.status_code == 200
    assert '<dialog class="gallery-lightbox"' in resp.text
    assert "data-lightbox-trigger" in resp.text
    assert "gallery-lightbox.js" in resp.text


async def test_hidden_images_excluded_from_lightbox_data(client, db):
    """Images marked show_in_gallery=False shouldn't appear in the JSON
    data the lightbox iterates over."""
    import json
    import re

    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(
        user_id=user.id, title="Bench", slug="bench",
        status=ProjectStatus.in_progress, is_public=True,
    ))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="visible-alpha.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="hidden-bravo.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    files = (await db.execute(select(ProjectFile))).scalars().all()
    visible = next(f for f in files if f.filename == "visible-alpha.png")
    hidden = next(f for f in files if f.filename == "hidden-bravo.png")
    token = await csrf_token(client, f"/u/alice/bench/files/{hidden.id}")
    await client.post(
        f"/u/alice/bench/files/{hidden.id}/gallery-visibility",
        data={"_csrf": token},
    )

    # Guest sees only the visible image in the lightbox data.
    client.cookies.clear()
    resp = await client.get("/u/alice/bench/gallery")
    assert resp.status_code == 200
    match = re.search(
        r'<script type="application/json" id="gallery-lightbox-data">(.*?)</script>',
        resp.text,
        re.DOTALL,
    )
    assert match
    data = json.loads(match.group(1))
    ids = {entry["id"] for entry in data}
    assert str(visible.id) in ids
    assert str(hidden.id) not in ids


async def test_lightbox_data_omitted_when_gallery_empty(client, db):
    """No visible images → no lightbox JSON / dialog (the empty-state card
    renders instead, no point shipping inert markup)."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")

    resp = await client.get("/u/alice/bench/gallery")
    assert resp.status_code == 200
    assert 'id="gallery-lightbox-data"' not in resp.text
    assert '<dialog class="gallery-lightbox"' not in resp.text


# ---------- cover image on cards ---------- #


async def test_project_card_renders_cover_image_when_set(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="hero.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    await client.post(f"/u/alice/bench/files/{file.id}/cover", data={"_csrf": token})

    resp = await client.get("/projects")
    assert resp.status_code == 200
    assert f"/u/alice/bench/files/{file.id}/thumb" in resp.text


async def test_project_card_has_no_cover_when_unset(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    resp = await client.get("/projects")
    assert resp.status_code == 200
    assert "/files/" not in resp.text or "/thumb" not in resp.text


async def test_cover_image_toggle_off_clears_cover(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="hero.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    await client.post(f"/u/alice/bench/files/{file.id}/cover", data={"_csrf": token})

    project = (await db.execute(select(Project))).scalar_one()
    assert project.cover_file_id == file.id

    # Second POST toggles off.
    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    await client.post(f"/u/alice/bench/files/{file.id}/cover", data={"_csrf": token})
    await db.refresh(project)
    assert project.cover_file_id is None


async def test_deleting_cover_image_clears_project_cover_fk(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="hero.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    await client.post(f"/u/alice/bench/files/{file.id}/cover", data={"_csrf": token})

    token = await csrf_token(client, "/u/alice/bench/files")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/delete", data={"_csrf": token}
    )
    assert resp.status_code == 302

    project = (await db.execute(select(Project))).scalar_one()
    assert project.cover_file_id is None


# ---------- cover crop ---------- #
#
# Four normalized floats on Project: (cover_crop_x, cover_crop_y,
# cover_crop_width, cover_crop_height). Stored only when the owner picks a
# specific 16:9 region via the cropper; NULL means "render the full image
# with object-fit: cover" (legacy behaviour). See routes/files.py.


# Valid 16:9 crop for the test helpers. The test image is 32x24 (4:3), so a
# 16:9 image-pixel region needs cw/ch = (16/9) * (24/32) = 4/3, NOT 16/9.
# (Normalized cw/ch = 16/9 only when the image itself is square; for any
# non-square image the saved coords have a different normalized ratio.)
_CROP_16_9 = {
    "crop_x": "0.1",
    "crop_y": "0.1",
    "crop_width": "0.6",
    "crop_height": "0.45",  # (0.6*32) / (0.45*24) = 19.2/10.8 = 16/9
}


async def _setup_alice_with_image(client, db, filename="hero.png"):
    """Shared scaffold: Alice, project "bench", one uploaded PNG, logged in."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename=filename, content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(
        select(ProjectFile).where(ProjectFile.filename == filename)
    )).scalar_one()
    return user, file


async def test_set_cover_with_crop_persists_normalized_coordinates(client, db):
    _, file = await _setup_alice_with_image(client, db)
    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/cover",
        data={"_csrf": token, **_CROP_16_9},
    )
    assert resp.status_code == 302

    project = (await db.execute(select(Project))).scalar_one()
    assert project.cover_file_id == file.id
    assert project.cover_crop_x == pytest.approx(0.1)
    assert project.cover_crop_y == pytest.approx(0.1)
    assert project.cover_crop_width == pytest.approx(0.6)
    assert project.cover_crop_height == pytest.approx(0.45)


async def test_set_cover_without_crop_leaves_columns_null(client, db):
    _, file = await _setup_alice_with_image(client, db)
    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/cover", data={"_csrf": token}
    )
    assert resp.status_code == 302

    project = (await db.execute(select(Project))).scalar_one()
    assert project.cover_file_id == file.id
    assert project.cover_crop_x is None
    assert project.cover_crop_y is None
    assert project.cover_crop_width is None
    assert project.cover_crop_height is None


async def test_changing_cover_resets_crop_to_null(client, db):
    user, file_a = await _setup_alice_with_image(client, db, filename="a.png")
    # Upload a second image so we can change the cover to a different file.
    await _upload(
        client, "/u/alice/bench/files",
        filename="b.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    file_b = (await db.execute(
        select(ProjectFile).where(ProjectFile.filename == "b.png")
    )).scalar_one()

    # Set cover to A with a crop.
    token = await csrf_token(client, f"/u/alice/bench/files/{file_a.id}")
    await client.post(
        f"/u/alice/bench/files/{file_a.id}/cover",
        data={"_csrf": token, **_CROP_16_9},
    )
    project = (await db.execute(select(Project))).scalar_one()
    assert project.cover_file_id == file_a.id
    assert project.cover_crop_width is not None

    # Set cover to B with NO crop — should nuke the crop entirely.
    token = await csrf_token(client, f"/u/alice/bench/files/{file_b.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{file_b.id}/cover", data={"_csrf": token}
    )
    assert resp.status_code == 302

    await db.refresh(project)
    assert project.cover_file_id == file_b.id
    assert project.cover_crop_x is None
    assert project.cover_crop_y is None
    assert project.cover_crop_width is None
    assert project.cover_crop_height is None


async def test_clear_cover_resets_crop_to_null(client, db):
    _, file = await _setup_alice_with_image(client, db)
    # Set cover with crop first.
    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    await client.post(
        f"/u/alice/bench/files/{file.id}/cover",
        data={"_csrf": token, **_CROP_16_9},
    )
    project = (await db.execute(select(Project))).scalar_one()
    assert project.cover_crop_width is not None

    # Now toggle off — POST /cover with no fields on the current cover.
    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/cover", data={"_csrf": token}
    )
    assert resp.status_code == 302

    await db.refresh(project)
    assert project.cover_file_id is None
    assert project.cover_crop_x is None
    assert project.cover_crop_y is None
    assert project.cover_crop_width is None
    assert project.cover_crop_height is None


async def test_cover_crop_validation_rejects_out_of_bounds(client, db):
    _, file = await _setup_alice_with_image(client, db)
    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/cover",
        data={
            "_csrf": token,
            "crop_x": "1.5",  # outside [0, 1]
            "crop_y": "0.1",
            "crop_width": "0.6",
            "crop_height": "0.3375",
        },
    )
    assert resp.status_code == 400


async def test_cover_crop_validation_rejects_wrong_aspect(client, db):
    _, file = await _setup_alice_with_image(client, db)
    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/cover",
        data={
            "_csrf": token,
            "crop_x": "0.0",
            "crop_y": "0.0",
            "crop_width": "0.5",
            "crop_height": "0.5",  # 1:1 — nowhere near 16:9
        },
    )
    assert resp.status_code == 400


async def test_cover_crop_route_adjusts_existing_crop(client, db):
    _, file = await _setup_alice_with_image(client, db)
    # First set cover with a crop.
    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    await client.post(
        f"/u/alice/bench/files/{file.id}/cover",
        data={"_csrf": token, **_CROP_16_9},
    )

    # Then re-adjust via /cover-crop.
    new_crop = {
        "crop_x": "0.2",
        "crop_y": "0.2",
        "crop_width": "0.4",
        "crop_height": "0.3",  # (0.4*32)/(0.3*24) = 16/9 for the 32x24 fixture
    }
    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/cover-crop",
        data={"_csrf": token, **new_crop},
    )
    assert resp.status_code == 302

    project = (await db.execute(select(Project))).scalar_one()
    assert project.cover_crop_x == pytest.approx(0.2)
    assert project.cover_crop_width == pytest.approx(0.4)


async def test_cover_crop_route_owner_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    db.add(Project(user_id=alice.id, title="Bench", slug="bench",
                   status=ProjectStatus.in_progress, is_public=True))
    await db.commit()

    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="hero.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()
    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    await client.post(
        f"/u/alice/bench/files/{file.id}/cover",
        data={"_csrf": token, **_CROP_16_9},
    )

    # Bob logs in and tries to change alice's crop — 404 (owner-scoped).
    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/cover-crop",
        data={"_csrf": token, **_CROP_16_9},
    )
    assert resp.status_code == 404


async def test_cover_crop_route_requires_file_to_be_current_cover(client, db):
    # Upload two images, set cover to A, then POST /cover-crop for B — 404.
    _, file_a = await _setup_alice_with_image(client, db, filename="a.png")
    await _upload(
        client, "/u/alice/bench/files",
        filename="b.png", content=_png_bytes(), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    file_b = (await db.execute(
        select(ProjectFile).where(ProjectFile.filename == "b.png")
    )).scalar_one()

    token = await csrf_token(client, f"/u/alice/bench/files/{file_a.id}")
    await client.post(
        f"/u/alice/bench/files/{file_a.id}/cover",
        data={"_csrf": token, **_CROP_16_9},
    )
    # Now B isn't the cover.
    token = await csrf_token(client, f"/u/alice/bench/files/{file_b.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{file_b.id}/cover-crop",
        data={"_csrf": token, **_CROP_16_9},
    )
    assert resp.status_code == 404


# ---------- folder rename + delete ---------- #


async def test_folder_rename_rewrites_paths_on_all_descendants(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.md", content=b"a", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="b.md", content=b"b", mime="text/markdown",
        extra_form={"path": "models/widgets"},
        csrf_path="/u/alice/bench",
    )

    token = await csrf_token(client, "/u/alice/bench/files/folder/edit?path=models")
    resp = await client.post(
        "/u/alice/bench/files/folder/rename",
        data={"_csrf": token, "old_path": "models", "new_path": "archive/legacy"},
    )
    assert resp.status_code == 302
    # Tree view has no `?path` scoping, so we drop back to the tree root.
    assert resp.headers["location"] == "/u/alice/bench/files"

    files = (await db.execute(select(ProjectFile).order_by(ProjectFile.filename))).scalars().all()
    paths = {f.filename: f.path for f in files}
    assert paths == {
        "a.md": "archive/legacy",
        "b.md": "archive/legacy/widgets",
    }


async def test_folder_rename_rejects_collision(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    # Start: models/a.md exists, and archive/a.md also exists — renaming
    # "models" -> "archive" would collide.
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.md", content=b"m", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.md", content=b"r", mime="text/markdown",
        extra_form={"path": "archive"},
        csrf_path="/u/alice/bench",
    )

    token = await csrf_token(client, "/u/alice/bench/files/folder/edit?path=models")
    resp = await client.post(
        "/u/alice/bench/files/folder/rename",
        data={"_csrf": token, "old_path": "models", "new_path": "archive"},
    )
    # 409 (Conflict) so the inline modal can show a targeted error.
    assert resp.status_code == 409
    assert "already exists" in resp.text
    # Nothing was moved.
    files = (await db.execute(select(ProjectFile))).scalars().all()
    assert {(f.path, f.filename) for f in files} == {
        ("models", "a.md"),
        ("archive", "a.md"),
    }


async def test_folder_delete_removes_every_descendant_file_and_blob(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.md", content=b"a", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="b.md", content=b"b", mime="text/markdown",
        extra_form={"path": "models/widgets"},
        csrf_path="/u/alice/bench",
    )
    # An unrelated root-level file that must NOT be deleted.
    await _upload(
        client, "/u/alice/bench/files",
        filename="keep.md", content=b"k", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    versions = (await db.execute(select(FileVersion))).scalars().all()
    blob_paths = [Path(get_storage().full_path(v.storage_path)) for v in versions]
    assert all(p.exists() for p in blob_paths)

    token = await csrf_token(client, "/u/alice/bench/files/folder/edit?path=models")
    resp = await client.post(
        "/u/alice/bench/files/folder/delete",
        data={"_csrf": token, "path": "models"},
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/bench/files"

    remaining = (await db.execute(select(ProjectFile))).scalars().all()
    assert [f.filename for f in remaining] == ["keep.md"]
    # The blobs that belonged to the deleted folder are gone; keep.md's
    # blob is still on disk.
    kept_blob = Path(get_storage().full_path((await db.execute(select(FileVersion))).scalar_one().storage_path))
    assert kept_blob.exists()


async def test_non_owner_cannot_rename_or_delete_folder(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    db.add(Project(
        user_id=alice.id, title="Bench", slug="bench",
        status=ProjectStatus.in_progress, is_public=True,
    ))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.md", content=b"a", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )

    await login(client, "bob")
    token = await csrf_token(client, "/projects")

    resp = await client.post(
        "/u/alice/bench/files/folder/rename",
        data={"_csrf": token, "old_path": "models", "new_path": "hijacked"},
    )
    assert resp.status_code == 404

    resp = await client.post(
        "/u/alice/bench/files/folder/delete",
        data={"_csrf": token, "path": "models"},
    )
    assert resp.status_code == 404

    # Alice's file is still in its original place.
    f = (await db.execute(select(ProjectFile))).scalar_one()
    assert f.path == "models"


async def test_folder_edit_form_404s_for_unknown_folder(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    resp = await client.get("/u/alice/bench/files/folder/edit?path=ghost")
    assert resp.status_code == 404


async def test_folder_edit_button_renders_for_owner_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    db.add(Project(
        user_id=alice.id, title="Bench", slug="bench",
        status=ProjectStatus.in_progress, is_public=True,
    ))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="x.md", content=b"x", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )

    # Owner sees the folder edit affordance linking to the folder edit page.
    resp = await client.get("/u/alice/bench/files")
    assert "/files/folder/edit?path=models" in resp.text

    # Guest (after clearing cookies) does not.
    client.cookies.clear()
    resp = await client.get("/u/alice/bench/files")
    assert "/files/folder/edit?path=" not in resp.text


# ---------- drag-and-drop move ---------- #


async def test_move_file_to_different_folder(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="moveable.md", content=b"x", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="anchor.md", content=b"x", mime="text/markdown",
        extra_form={"path": "archive"},
        csrf_path="/u/alice/bench",
    )
    moveable = next(
        f for f in (await db.execute(select(ProjectFile))).scalars().all()
        if f.filename == "moveable.md"
    )

    token = await csrf_token(client, "/u/alice/bench/files")
    resp = await client.post(
        "/u/alice/bench/files/move",
        data={
            "_csrf": token,
            "source_kind": "file",
            "source_id": str(moveable.id),
            "destination_path": "archive",
        },
    )
    assert resp.status_code == 204
    await db.refresh(moveable)
    assert moveable.path == "archive"


async def test_move_file_to_root(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="buried.md", content=b"x", mime="text/markdown",
        extra_form={"path": "deep/nested"},
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    token = await csrf_token(client, "/u/alice/bench/files")
    resp = await client.post(
        "/u/alice/bench/files/move",
        data={
            "_csrf": token,
            "source_kind": "file",
            "source_id": str(file.id),
            "destination_path": "",
        },
    )
    assert resp.status_code == 204
    await db.refresh(file)
    assert file.path == ""


async def test_move_file_rejects_collision(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="shared-name.md", content=b"a", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="shared-name.md", content=b"b", mime="text/markdown",
        extra_form={"path": "archive"},
        csrf_path="/u/alice/bench",
    )
    source = next(
        f for f in (await db.execute(select(ProjectFile))).scalars().all()
        if f.path == "models"
    )

    token = await csrf_token(client, "/u/alice/bench/files")
    resp = await client.post(
        "/u/alice/bench/files/move",
        data={
            "_csrf": token,
            "source_kind": "file",
            "source_id": str(source.id),
            "destination_path": "archive",
        },
    )
    assert resp.status_code == 409
    await db.refresh(source)
    assert source.path == "models"  # unchanged


async def test_move_folder_to_new_parent(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="x.md", content=b"x", mime="text/markdown",
        extra_form={"path": "models/widgets"},
        csrf_path="/u/alice/bench",
    )

    token = await csrf_token(client, "/u/alice/bench/files")
    resp = await client.post(
        "/u/alice/bench/files/move",
        data={
            "_csrf": token,
            "source_kind": "folder",
            "source_path": "models/widgets",
            "destination_path": "archive",
        },
    )
    assert resp.status_code == 204
    f = (await db.execute(select(ProjectFile))).scalar_one()
    # Folder basename preserved — "widgets" landed inside "archive".
    assert f.path == "archive/widgets"


async def test_move_folder_into_itself_is_rejected(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="x.md", content=b"x", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )

    token = await csrf_token(client, "/u/alice/bench/files")
    # Drop "models" onto "models" (itself) or onto a descendant.
    resp = await client.post(
        "/u/alice/bench/files/move",
        data={
            "_csrf": token,
            "source_kind": "folder",
            "source_path": "models",
            "destination_path": "models",
        },
    )
    assert resp.status_code == 400


async def test_move_folder_into_descendant_is_rejected(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="x.md", content=b"x", mime="text/markdown",
        extra_form={"path": "models/widgets"},
        csrf_path="/u/alice/bench",
    )

    token = await csrf_token(client, "/u/alice/bench/files")
    resp = await client.post(
        "/u/alice/bench/files/move",
        data={
            "_csrf": token,
            "source_kind": "folder",
            "source_path": "models",
            "destination_path": "models/widgets",
        },
    )
    assert resp.status_code == 400
    # Path unchanged.
    f = (await db.execute(select(ProjectFile))).scalar_one()
    assert f.path == "models/widgets"


async def test_non_owner_cannot_move(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    db.add(Project(
        user_id=alice.id, title="Bench", slug="bench",
        status=ProjectStatus.in_progress, is_public=True,
    ))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="locked.md", content=b"x", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        "/u/alice/bench/files/move",
        data={
            "_csrf": token,
            "source_kind": "file",
            "source_id": str(file.id),
            "destination_path": "bob-put-it-here",
        },
    )
    assert resp.status_code == 404
    await db.refresh(file)
    assert file.path == ""


async def test_file_tree_rows_are_draggable_for_owner_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    db.add(Project(
        user_id=alice.id, title="Bench", slug="bench",
        status=ProjectStatus.in_progress, is_public=True,
    ))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="x.md", content=b"x", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )

    # Owner: row carries draggable=true + move URL + root drop target attr.
    resp = await client.get("/u/alice/bench/files")
    owner_body = resp.text
    # Find the file row and confirm it has draggable=true.
    row_start = owner_body.index('class="file-tree-row-file"')
    row_open = owner_body.rfind("<tr", 0, row_start)
    row_end = owner_body.index(">", row_start)
    row_tag = owner_body[row_open:row_end + 1]
    assert 'draggable="true"' in row_tag
    assert 'data-move-url=' in owner_body
    assert 'data-root-drop-target' in owner_body

    # Guest: file row is NOT draggable and there's no move URL wired in.
    client.cookies.clear()
    resp = await client.get("/u/alice/bench/files")
    guest_body = resp.text
    row_start = guest_body.index('class="file-tree-row-file"')
    row_open = guest_body.rfind("<tr", 0, row_start)
    row_end = guest_body.index(">", row_start)
    row_tag = guest_body[row_open:row_end + 1]
    assert 'draggable="true"' not in row_tag
    assert 'data-move-url=' not in guest_body


async def test_file_row_shows_download_action_for_any_viewer(client, db):
    """Download isn't owner-gated — guests on a public project get the
    icon too (same as the existing /download route)."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(
        user_id=user.id, title="Bench", slug="bench",
        status=ProjectStatus.in_progress, is_public=True,
    ))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="public.md", content=b"x", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    # Owner sees the download link.
    resp = await client.get("/u/alice/bench/files")
    assert f'href="/u/alice/bench/files/{file.id}/download"' in resp.text

    # Guest also sees the download link on a public project.
    client.cookies.clear()
    resp = await client.get("/u/alice/bench/files")
    assert f'href="/u/alice/bench/files/{file.id}/download"' in resp.text


async def test_file_rows_render_edit_and_delete_actions_for_owner(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="x.md", content=b"x", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    resp = await client.get("/u/alice/bench/files")
    body = resp.text
    # Edit action links to the existing edit page.
    assert f'/files/{file.id}/edit"' in body
    # Delete action is a form targeting the existing delete route.
    assert f'action="/u/alice/bench/files/{file.id}/delete"' in body
    assert 'data-confirm' in body


# ---------- zip download ---------- #


async def test_download_zip_whole_project_preserves_full_paths(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="readme.md", content=b"root-file", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.md", content=b"models-a", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="b.md", content=b"widgets-b", mime="text/markdown",
        extra_form={"path": "models/widgets"},
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/files/download-zip")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    # `-files` infix leaves `{slug}.zip` reserved for a future whole-project
    # export that would include metadata alongside the blobs.
    assert "bench-files.zip" in resp.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
        assert names == {"readme.md", "models/a.md", "models/widgets/b.md"}
        assert zf.read("models/widgets/b.md") == b"widgets-b"


async def test_download_zip_folder_strips_own_prefix(client, db):
    """A folder zip opens with that folder's contents at the top, not
    nested under its full project path."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="top.md", content=b"at-root", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="direct.md", content=b"direct-in-widgets", mime="text/markdown",
        extra_form={"path": "models/widgets"},
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="nested.md", content=b"nested-under-widgets", mime="text/markdown",
        extra_form={"path": "models/widgets/sub"},
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/files/download-zip?path=models/widgets")
    assert resp.status_code == 200
    # Filename uses dash-joined folder segments, with the `-files-` infix.
    assert "bench-files-models-widgets.zip" in resp.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
        assert names == {"direct.md", "sub/nested.md"}
        # The root-level file shouldn't be in a folder zip.
        assert "top.md" not in names


async def test_download_zip_includes_only_latest_version(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="notes.md", content=b"v1-content", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    # Second upload to same (path, filename) creates v2.
    await _upload(
        client, "/u/alice/bench/files",
        filename="notes.md", content=b"v2-updated", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/files/download-zip")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.read("notes.md") == b"v2-updated"


async def test_download_zip_404s_when_project_has_no_files(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    resp = await client.get("/u/alice/bench/files/download-zip")
    assert resp.status_code == 404


async def test_download_zip_404s_for_unknown_folder(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="x.md", content=b"x", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    resp = await client.get("/u/alice/bench/files/download-zip?path=archive")
    assert resp.status_code == 404


async def test_download_zip_guest_works_on_public_project(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(
        user_id=user.id, title="Bench", slug="bench",
        status=ProjectStatus.in_progress, is_public=True,
    ))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="public.md", content=b"public", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    client.cookies.clear()
    resp = await client.get("/u/alice/bench/files/download-zip")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.read("public.md") == b"public"


async def test_download_zip_guest_404s_on_private_project(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="secret.md", content=b"s", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    client.cookies.clear()
    resp = await client.get("/u/alice/bench/files/download-zip")
    assert resp.status_code == 404


async def test_files_page_shows_download_all_and_folder_zip_links(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="x.md", content=b"x", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    resp = await client.get("/u/alice/bench/files")
    body = resp.text
    # Header has a Download all action.
    assert 'href="/u/alice/bench/files/download-zip"' in body
    assert "Download all" in body
    # Folder row has its own download-zip action.
    assert 'href="/u/alice/bench/files/download-zip?path=models"' in body


# ---------- header controls + version column ---------- #


async def test_files_tab_omits_version_column(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="x.md", content=b"x", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    resp = await client.get("/u/alice/bench/files")
    body = resp.text
    header_block = body[body.index("<thead"):body.index("</thead>")]
    assert ">Version<" not in header_block
    # Sanity — Name / Size / Modified are still present.
    assert ">Name<" in header_block
    assert ">Size<" in header_block
    assert ">Modified<" in header_block


async def test_files_tab_shows_expand_collapse_buttons_when_folders_exist(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.md", content=b"x", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    resp = await client.get("/u/alice/bench/files")
    body = resp.text
    assert "data-file-tree-expand-all" in body
    assert "data-file-tree-collapse-all" in body
    assert "Expand all" in body
    assert "Collapse all" in body


async def test_files_tab_hides_expand_collapse_buttons_when_no_folders(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.md", content=b"x", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    resp = await client.get("/u/alice/bench/files")
    body = resp.text
    # The button labels are unique to the rendered <button> markup —
    # the JS source contains the data attribute strings as selectors,
    # but never the visible labels, so this distinguishes cleanly.
    assert "Expand all" not in body
    assert "Collapse all" not in body


# ---------- OS-safe enforcement at the route level ---------- #


async def test_upload_rejects_os_unsafe_filename(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    resp = await _upload(
        client,
        "/u/alice/bench/files",
        filename="bad:colon.md",
        content=b"x",
        mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 400
    # No file row created.
    files = (await db.execute(select(ProjectFile))).scalars().all()
    assert files == []


async def test_upload_rejects_reserved_windows_name(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    resp = await _upload(
        client,
        "/u/alice/bench/files",
        filename="CON.txt",
        content=b"x",
        mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 400


async def test_upload_rejects_unsafe_folder_segment(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    resp = await _upload(
        client,
        "/u/alice/bench/files",
        filename="ok.md",
        content=b"x",
        mime="text/markdown",
        extra_form={"path": "good/bad?segment"},
        csrf_path="/u/alice/bench",
    )
    assert resp.status_code == 400


# ---------- DnD upload (POST /files JSON mode) ---------- #


async def test_upload_returns_204_json_on_success(client, db):
    """The fetch-based DnD uploader sends Accept: application/json so
    it can stay on the page; the server returns 204 instead of the HTML
    redirect that browser form submits use."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")

    token = await csrf_token(client, "/u/alice/bench")
    files = {"upload": ("dropped.md", b"hello", "text/markdown")}
    resp = await client.post(
        "/u/alice/bench/files",
        data={"_csrf": token, "path": "drop-target", "description": ""},
        files=files,
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 204
    f = (await db.execute(select(ProjectFile))).scalar_one()
    assert f.filename == "dropped.md"
    assert f.path == "drop-target"


async def test_upload_returns_400_json_on_unsafe_filename(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")

    token = await csrf_token(client, "/u/alice/bench")
    files = {"upload": ("bad:colon.md", b"hello", "text/markdown")}
    resp = await client.post(
        "/u/alice/bench/files",
        data={"_csrf": token, "path": "", "description": ""},
        files=files,
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 400
    payload = resp.json()
    assert "cannot contain" in payload["detail"]


async def test_upload_via_dnd_to_nested_folder_creates_path(client, db):
    """When the client folder-walks a dropped folder, it computes the
    final `path` as base + relative — server just trusts it (and runs
    the same OS-safe validation as any other upload)."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")

    token = await csrf_token(client, "/u/alice/bench")
    files = {"upload": ("nested.md", b"x", "text/markdown")}
    resp = await client.post(
        "/u/alice/bench/files",
        data={
            "_csrf": token,
            "path": "drop-into/dropped-folder",
            "description": "",
        },
        files=files,
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 204
    f = (await db.execute(select(ProjectFile))).scalar_one()
    assert f.path == "drop-into/dropped-folder"
    assert f.filename == "nested.md"


async def test_upload_collision_creates_new_version(client, db):
    """Dropping a file with the same path+name as an existing one bumps
    the version instead of failing — this is the documented DnD-replace
    behaviour."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")

    token = await csrf_token(client, "/u/alice/bench")
    files1 = {"upload": ("notes.md", b"v1", "text/markdown")}
    await client.post(
        "/u/alice/bench/files",
        data={"_csrf": token, "path": "", "description": ""},
        files=files1,
        headers={"Accept": "application/json"},
    )
    token = await csrf_token(client, "/u/alice/bench")
    files2 = {"upload": ("notes.md", b"v2-replacement", "text/markdown")}
    resp = await client.post(
        "/u/alice/bench/files",
        data={"_csrf": token, "path": "", "description": ""},
        files=files2,
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 204
    rows = (await db.execute(select(ProjectFile))).scalars().all()
    assert len(rows) == 1  # same row, new version
    versions = (await db.execute(select(FileVersion))).scalars().all()
    assert {v.version_number for v in versions} == {1, 2}


async def test_files_tab_advertises_upload_url_for_dnd(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="seed.md", content=b"x", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    resp = await client.get("/u/alice/bench/files")
    body = resp.text
    # Table carries the upload URL the JS will POST each dropped file to.
    assert 'data-upload-url="/u/alice/bench/files"' in body
    # And the polite live region for upload progress.
    assert 'data-upload-status' in body


# ---------- modified timestamp tooltips ---------- #


async def test_modified_cells_use_time_element_with_iso_datetime(client, db):
    """Each Modified cell wraps its date in a <time datetime> element so
    client-side JS can localize the tooltip to the viewer's timezone."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="x.md", content=b"x", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )

    resp = await client.get("/u/alice/bench/files")
    body = resp.text
    # Both file and folder rows render <time datetime="..."> elements.
    assert "<time datetime=" in body
    # Default UTC title is present (JS overrides it client-side).
    assert "UTC</time>" in body or 'UTC">' in body


# ---------- edit modals: JSON responses ---------- #


async def test_file_edit_returns_204_json_on_success(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="before.md", content=b"x", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    token = await csrf_token(client, "/u/alice/bench/files")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}",
        data={
            "_csrf": token,
            "path": "renamed-folder",
            "filename": "after.md",
            "description": "fresh desc",
        },
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 204
    await db.refresh(file)
    assert file.path == "renamed-folder"
    assert file.filename == "after.md"
    assert file.description == "fresh desc"


async def test_file_edit_returns_409_json_on_collision(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="target.md", content=b"a", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="source.md", content=b"b", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    source = next(
        f for f in (await db.execute(select(ProjectFile))).scalars().all()
        if f.filename == "source.md"
    )

    token = await csrf_token(client, "/u/alice/bench/files")
    resp = await client.post(
        f"/u/alice/bench/files/{source.id}",
        data={
            "_csrf": token,
            "path": "",
            "filename": "target.md",
            "description": "",
        },
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 409
    payload = resp.json()
    assert "already exists" in payload["detail"]


async def test_folder_rename_returns_204_json_on_success(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="x.md", content=b"x", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )

    token = await csrf_token(client, "/u/alice/bench/files")
    resp = await client.post(
        "/u/alice/bench/files/folder/rename",
        data={"_csrf": token, "old_path": "models", "new_path": "archive/legacy"},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 204
    f = (await db.execute(select(ProjectFile))).scalar_one()
    assert f.path == "archive/legacy"


async def test_folder_rename_returns_409_json_on_collision(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.md", content=b"1", mime="text/markdown",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.md", content=b"2", mime="text/markdown",
        extra_form={"path": "archive"},
        csrf_path="/u/alice/bench",
    )

    token = await csrf_token(client, "/u/alice/bench/files")
    resp = await client.post(
        "/u/alice/bench/files/folder/rename",
        data={"_csrf": token, "old_path": "models", "new_path": "archive"},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 409
    payload = resp.json()
    assert "already exists" in payload["detail"]


async def test_file_edit_html_fallback_still_works_without_accept_json(client, db):
    """Without Accept: application/json, the existing HTML redirect flow
    is preserved so the full edit page still functions."""
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="x.md", content=b"x", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}/edit")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}",
        data={
            "_csrf": token,
            "path": "",
            "filename": "still-html.md",
            "description": "",
        },
    )
    assert resp.status_code == 302
    await db.refresh(file)
    assert file.filename == "still-html.md"


# ---------- edit modals: DOM plumbing ---------- #


async def test_files_tab_renders_file_edit_modal_for_owner(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.md", content=b"x", mime="text/markdown",
        extra_form={"path": "nested/folder"},
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    resp = await client.get("/u/alice/bench/files")
    body = resp.text
    assert "data-file-edit-modal" in body
    assert "data-folder-edit-modal" in body
    # Pencil carries the pre-filled full path (folder + filename) for the modal.
    assert 'data-file-fullpath="nested/folder/a.md"' in body
    # And the submit URL for the fetch POST.
    assert f'data-file-submit-url="/u/alice/bench/files/{file.id}"' in body


async def test_files_tab_renders_no_modals_for_guest(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(
        user_id=user.id, title="Bench", slug="bench",
        status=ProjectStatus.in_progress, is_public=True,
    ))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.md", content=b"x", mime="text/markdown",
        csrf_path="/u/alice/bench",
    )
    client.cookies.clear()
    resp = await client.get("/u/alice/bench/files")
    # Guests don't get the file/folder edit dialogs. The JS source still
    # contains selector strings like `[data-file-edit-modal]`, so we
    # check for the attribute as rendered on an opening tag
    # (`data-file-edit-modal>`) rather than anywhere in the body — the
    # shared _confirm_modal also always renders a <dialog>.
    assert "data-file-edit-modal>" not in resp.text
    assert "data-folder-edit-modal>" not in resp.text


# ---------- file detail breadcrumb casing ---------- #


async def test_file_detail_breadcrumb_preserves_filename_casing(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="MixedCase-File.MD", content=b"x", mime="text/markdown",
        extra_form={"path": "Models/Widgets"},
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    resp = await client.get(f"/u/alice/bench/files/{file.id}")
    # The filename and folder path render with their original casing —
    # NOT forced uppercase by the `.meta` utility. Path is now split into
    # per-segment crumbs so each segment shows independently.
    assert "MixedCase-File.MD" in resp.text
    assert ">Models<" in resp.text
    assert ">Widgets<" in resp.text
    # Locate the breadcrumb nav specifically (the page also has a tab nav
    # at the top — `aria-label="File location"` distinguishes the crumb).
    crumb_start = resp.text.index('aria-label="File location"')
    crumb_end = resp.text.index("</nav>", crumb_start)
    crumb = resp.text[crumb_start:crumb_end]
    # The filename span inside the crumb should not carry the `meta` class
    # (which is what was forcing uppercase before).
    filename_marker = crumb.index("MixedCase-File.MD")
    span_open = crumb.rfind("<span", 0, filename_marker)
    span_tag = crumb[span_open:filename_marker]
    assert 'class="meta' not in span_tag


# ---------- markdown rewriter ---------- #


def test_markdown_rewriter_resolves_known_file_to_canonical_url():
    html = '<p><a href="files/models/widget.stl">widget</a></p>'

    def lookup(path, name):
        return "abc123" if (path, name) == ("models", "widget.stl") else None

    out = rewrite_project_file_links(html, "alice", "bench", lookup)
    # Links go to the file DETAIL page (not /download) so the reader gets
    # metadata, versions, and preview instead of a forced download.
    assert 'href="/u/alice/bench/files/abc123"' in out


def test_markdown_rewriter_falls_back_to_browser_when_unknown():
    html = '<p><a href="files/models/widget.stl">widget</a></p>'
    out = rewrite_project_file_links(html, "alice", "bench", lambda p, n: None)
    assert 'href="/u/alice/bench/files?path=models"' in out


def test_markdown_rewriter_root_path_omits_query():
    html = '<p><a href="files/widget.stl">widget</a></p>'
    out = rewrite_project_file_links(html, "alice", "bench", lambda p, n: None)
    assert 'href="/u/alice/bench/files"' in out


def test_markdown_rewriter_leaves_other_links_alone():
    html = '<p><a href="https://example.com">ext</a></p>'
    out = rewrite_project_file_links(html, "alice", "bench", lambda p, n: None)
    assert 'href="https://example.com"' in out


# ---------- delete / restore individual version ---------- #


async def _make_versioned_file(client, *, filenames_and_bodies, project_slug="bench"):
    """Helper — upload each (filename, body) pair in order to the same path
    so they collapse into a single file with sequential versions."""
    for filename, body in filenames_and_bodies:
        await _upload(
            client,
            f"/u/alice/{project_slug}/files",
            filename=filename,
            content=body,
            mime="text/plain",
            csrf_path=f"/u/alice/{project_slug}",
        )


async def test_delete_non_current_version_removes_row_and_blob(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")

    await _make_versioned_file(
        client, filenames_and_bodies=[("notes.txt", b"alpha"), ("notes.txt", b"beta")]
    )

    file = (await db.execute(select(ProjectFile))).scalar_one()
    versions = (
        await db.execute(
            select(FileVersion)
            .where(FileVersion.file_id == file.id)
            .order_by(FileVersion.version_number)
        )
    ).scalars().all()
    assert [v.version_number for v in versions] == [1, 2]
    v1_blob = Path(get_storage().full_path(versions[0].storage_path))
    assert v1_blob.exists()

    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/version/1/delete",
        data={"_csrf": token},
    )
    assert resp.status_code == 302

    remaining = (
        await db.execute(
            select(FileVersion).where(FileVersion.file_id == file.id)
        )
    ).scalars().all()
    assert [v.version_number for v in remaining] == [2]
    assert not v1_blob.exists()

    # v2 still current + downloadable.
    await db.refresh(file)
    assert file.current_version_id == remaining[0].id
    resp = await client.get(f"/u/alice/bench/files/{file.id}/download")
    assert resp.status_code == 200
    assert resp.content == b"beta"


async def test_delete_current_version_blocked_when_others_exist(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")

    await _make_versioned_file(
        client, filenames_and_bodies=[("notes.txt", b"alpha"), ("notes.txt", b"beta")]
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/version/2/delete",
        data={"_csrf": token},
    )
    assert resp.status_code == 400
    assert "Restore another version first" in resp.text

    # v2 still exists.
    versions = (
        await db.execute(
            select(FileVersion).where(FileVersion.file_id == file.id)
        )
    ).scalars().all()
    assert {v.version_number for v in versions} == {1, 2}


async def test_delete_only_remaining_version_blocked(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")

    await _upload(
        client, "/u/alice/bench/files",
        filename="solo.txt", content=b"alpha", mime="text/plain",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/version/1/delete",
        data={"_csrf": token},
    )
    assert resp.status_code == 400
    assert "Delete File" in resp.text or "Delete file" in resp.text

    versions = (
        await db.execute(
            select(FileVersion).where(FileVersion.file_id == file.id)
        )
    ).scalars().all()
    assert [v.version_number for v in versions] == [1]


async def test_delete_version_owner_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    db.add(Project(user_id=alice.id, title="Bench", slug="bench",
                   status=ProjectStatus.in_progress, is_public=True))
    await db.commit()

    await login(client, "alice")
    await _make_versioned_file(
        client, filenames_and_bodies=[("notes.txt", b"alpha"), ("notes.txt", b"beta")]
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/version/1/delete",
        data={"_csrf": token},
    )
    assert resp.status_code == 404

    # v1 still intact.
    versions = (
        await db.execute(
            select(FileVersion).where(FileVersion.file_id == file.id)
        )
    ).scalars().all()
    assert [v.version_number for v in versions] == [1, 2]


async def test_restore_version_creates_new_current_with_copied_blob(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")

    await _make_versioned_file(
        client, filenames_and_bodies=[("notes.txt", b"alpha"), ("notes.txt", b"beta")]
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/version/1/restore",
        data={"_csrf": token},
    )
    assert resp.status_code == 302

    versions = (
        await db.execute(
            select(FileVersion)
            .where(FileVersion.file_id == file.id)
            .order_by(FileVersion.version_number)
        )
    ).scalars().all()
    assert [v.version_number for v in versions] == [1, 2, 3]

    v1, v2, v3 = versions
    await db.refresh(file)
    assert file.current_version_id == v3.id
    assert v3.changelog == "Restored from v1"
    assert v3.size_bytes == v1.size_bytes
    assert v3.checksum == v1.checksum
    # Copied (not symlinked) — two distinct blobs on disk.
    v1_blob = Path(get_storage().full_path(v1.storage_path))
    v3_blob = Path(get_storage().full_path(v3.storage_path))
    assert v1_blob.exists() and v3_blob.exists()
    assert v1_blob != v3_blob

    # Download returns the restored (v1's) content.
    resp = await client.get(f"/u/alice/bench/files/{file.id}/download")
    assert resp.status_code == 200
    assert resp.content == b"alpha"

    # v1 row still intact and downloadable by explicit version.
    resp = await client.get(f"/u/alice/bench/files/{file.id}/download?v=1")
    assert resp.status_code == 200
    assert resp.content == b"alpha"


async def test_restore_version_for_image_regenerates_thumbnail(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")

    # Two image versions at the same path.
    await _upload(
        client, "/u/alice/bench/files",
        filename="photo.png", content=_png_bytes(100, 80), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    await _upload(
        client, "/u/alice/bench/files",
        filename="photo.png", content=_png_bytes(60, 40), mime="image/png",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/version/1/restore",
        data={"_csrf": token},
    )
    assert resp.status_code == 302

    versions = (
        await db.execute(
            select(FileVersion)
            .where(FileVersion.file_id == file.id)
            .order_by(FileVersion.version_number)
        )
    ).scalars().all()
    v3 = versions[-1]
    assert v3.version_number == 3
    assert v3.width == 100
    assert v3.height == 80
    assert v3.thumbnail_path is not None
    assert Path(get_storage().full_path(v3.thumbnail_path)).exists()


async def test_restore_current_version_rejected(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(Project(user_id=user.id, title="Bench", slug="bench", status=ProjectStatus.idea))
    await db.commit()
    await login(client, "alice")

    await _make_versioned_file(
        client, filenames_and_bodies=[("notes.txt", b"alpha"), ("notes.txt", b"beta")]
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/version/2/restore",
        data={"_csrf": token},
    )
    assert resp.status_code == 400
    assert "already the latest" in resp.text

    # No new version row was created.
    versions = (
        await db.execute(
            select(FileVersion).where(FileVersion.file_id == file.id)
        )
    ).scalars().all()
    assert {v.version_number for v in versions} == {1, 2}


async def test_restore_version_owner_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    db.add(Project(user_id=alice.id, title="Bench", slug="bench",
                   status=ProjectStatus.in_progress, is_public=True))
    await db.commit()

    await login(client, "alice")
    await _make_versioned_file(
        client, filenames_and_bodies=[("notes.txt", b"alpha"), ("notes.txt", b"beta")]
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}/version/1/restore",
        data={"_csrf": token},
    )
    assert resp.status_code == 404

    # No new version was created.
    versions = (
        await db.execute(
            select(FileVersion).where(FileVersion.file_id == file.id)
        )
    ).scalars().all()
    assert {v.version_number for v in versions} == {1, 2}


# ---------- markdown rename-tracking ---------- #
#
# When a file/folder is renamed or moved, the matching `files/<old>` links
# in the project's description and updates get patched to the new path
# (unless the opt-out checkbox is unchecked on the form surfaces — DnD
# has no form and always rewrites).


async def _seed_project_with_refs(
    db,
    *,
    username: str = "alice",
    email: str = "alice@test.com",
    description: str,
    update_content: str | None = None,
    slug: str = "bench",
) -> tuple[object, object]:
    """Create a user + project with description and optional update body.

    Returns (user, project).
    """
    user = await make_user(db, email=email, username=username)
    project = Project(
        user_id=user.id,
        title="Bench",
        slug=slug,
        status=ProjectStatus.idea,
        description=description,
    )
    db.add(project)
    await db.flush()
    if update_content is not None:
        db.add(ProjectUpdate(project_id=project.id, content=update_content))
    await db.commit()
    return user, project


async def test_file_rename_updates_description_and_updates(client, db):
    _, project = await _seed_project_with_refs(
        db,
        description="See [orig](files/a.stl) for details.",
        update_content="Also [orig](files/a.stl) is here.",
    )
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.stl", content=b"x", mime="application/octet-stream",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}/edit")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}",
        data={
            "_csrf": token,
            "filename": "b.stl",
            "path": "",
            "description": "",
            "update_refs": "1",
        },
    )
    assert resp.status_code == 302

    await db.refresh(project)
    assert project.description == "See [orig](files/b.stl) for details."
    updates = (
        await db.execute(select(ProjectUpdate))
    ).scalars().all()
    assert updates[0].content == "Also [orig](files/b.stl) is here."

    # The flash notice should follow along on the redirected GET page.
    detail = await client.get(resp.headers["location"])
    assert "Updated 2 markdown references" in detail.text


async def test_file_rename_without_update_refs_leaves_markdown_alone(client, db):
    _, project = await _seed_project_with_refs(
        db,
        description="See [orig](files/a.stl) for details.",
        update_content="Update ref [x](files/a.stl).",
    )
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.stl", content=b"x", mime="application/octet-stream",
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    token = await csrf_token(client, f"/u/alice/bench/files/{file.id}/edit")
    resp = await client.post(
        f"/u/alice/bench/files/{file.id}",
        data={
            "_csrf": token,
            "filename": "b.stl",
            "path": "",
            "description": "",
            # update_refs intentionally omitted — unchecked checkbox
        },
    )
    assert resp.status_code == 302
    await db.refresh(project)
    assert project.description == "See [orig](files/a.stl) for details."
    update = (await db.execute(select(ProjectUpdate))).scalar_one()
    assert update.content == "Update ref [x](files/a.stl)."

    detail = await client.get(resp.headers["location"])
    # Flash says "File renamed" but does NOT mention reference count.
    assert "File renamed" in detail.text
    assert "Updated" not in detail.text or "markdown references" not in detail.text


async def test_folder_rename_updates_all_refs_across_files(client, db):
    _, project = await _seed_project_with_refs(
        db,
        description=(
            "See [a](files/models/a.stl) and [b](files/models/b.stl)."
        ),
    )
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.stl", content=b"x", mime="application/octet-stream",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )

    token = await csrf_token(
        client, "/u/alice/bench/files/folder/edit?path=models"
    )
    resp = await client.post(
        "/u/alice/bench/files/folder/rename",
        data={
            "_csrf": token,
            "old_path": "models",
            "new_path": "stl",
            "update_refs": "1",
        },
    )
    assert resp.status_code == 302
    await db.refresh(project)
    assert project.description == (
        "See [a](files/stl/a.stl) and [b](files/stl/b.stl)."
    )

    followed = await client.get(resp.headers["location"])
    assert "Updated 2 markdown references" in followed.text


async def test_dnd_move_updates_refs(client, db):
    _, project = await _seed_project_with_refs(
        db,
        description="Link: [x](files/models/a.stl)",
    )
    await login(client, "alice")
    await _upload(
        client, "/u/alice/bench/files",
        filename="a.stl", content=b"x", mime="application/octet-stream",
        extra_form={"path": "models"},
        csrf_path="/u/alice/bench",
    )
    file = (await db.execute(select(ProjectFile))).scalar_one()

    token = await csrf_token(client, "/u/alice/bench/files")
    resp = await client.post(
        "/u/alice/bench/files/move",
        data={
            "_csrf": token,
            "source_kind": "file",
            "source_id": str(file.id),
            "destination_path": "archive",
            # No update_refs form field — DnD always rewrites by design.
        },
    )
    assert resp.status_code == 204

    await db.refresh(project)
    assert project.description == "Link: [x](files/archive/a.stl)"


async def test_rename_only_touches_the_renaming_project(client, db):
    # Two separate projects owned by the same user, both with the same
    # `files/a.stl` ref in their descriptions. Renaming the file inside
    # project1 must NOT touch project2's markdown — the rewrite is
    # scoped to the project the file lives in.
    user = await make_user(db, email="alice@test.com", username="alice")
    p1 = Project(
        user_id=user.id, title="One", slug="one", status=ProjectStatus.idea,
        description="[ref](files/a.stl)",
    )
    p2 = Project(
        user_id=user.id, title="Two", slug="two", status=ProjectStatus.idea,
        description="[ref](files/a.stl)",
    )
    db.add_all([p1, p2])
    await db.commit()
    await login(client, "alice")
    await _upload(
        client, "/u/alice/one/files",
        filename="a.stl", content=b"x", mime="application/octet-stream",
        csrf_path="/u/alice/one",
    )
    file = (
        await db.execute(select(ProjectFile).where(ProjectFile.project_id == p1.id))
    ).scalar_one()

    token = await csrf_token(client, f"/u/alice/one/files/{file.id}/edit")
    resp = await client.post(
        f"/u/alice/one/files/{file.id}",
        data={
            "_csrf": token,
            "filename": "b.stl",
            "path": "",
            "description": "",
            "update_refs": "1",
        },
    )
    assert resp.status_code == 302

    await db.refresh(p1)
    await db.refresh(p2)
    assert p1.description == "[ref](files/b.stl)"
    assert p2.description == "[ref](files/a.stl)"  # untouched
