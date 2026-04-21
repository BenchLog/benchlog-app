"""Tests for project Forks — hard-copy fork of a public project into the
caller's namespace.

Covers the route gates (public-only, non-owner-only, auth required), the
helper-level copy semantics (updates, links, files + versions, blob copy,
`fork_of` relation), ancestry columns (`is_fork` + `forked_from_id`), and
the detail-page UX (fork button visibility, "Forked from …" header).
"""

import functools
import io
import shutil

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from benchlog.config import settings
from benchlog.models import (
    FileVersion,
    LinkType,
    Project,
    ProjectFile,
    ProjectLink,
    ProjectRelation,
    ProjectStatus,
    ProjectUpdate,
    RelationType,
)
from benchlog.storage import get_storage
from tests.conftest import csrf_token, login, make_user


# ---------- fixtures / helpers ---------- #


@pytest.fixture(autouse=True)
def _isolate_storage(tmp_path, monkeypatch):
    """Each test gets a fresh storage root so cross-test blobs don't leak."""
    monkeypatch.setattr(settings, "storage_local_path", str(tmp_path / "files"))
    get_storage.cache_clear()
    yield
    get_storage.cache_clear()
    shutil.rmtree(tmp_path / "files", ignore_errors=True)


@functools.cache
def _png_bytes(width: int = 32, height: int = 24) -> bytes:
    img = Image.new("RGB", (width, height), color=(180, 80, 60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _upload(
    client,
    url: str,
    *,
    filename: str,
    content: bytes,
    mime: str = "application/octet-stream",
    extra_form: dict | None = None,
    csrf_path: str,
):
    token = await csrf_token(client, csrf_path)
    data = {"_csrf": token, **(extra_form or {})}
    files = {"upload": (filename, content, mime)}
    return await client.post(url, data=data, files=files)


async def _make_project(
    db, user, *, title="Src", slug="src", is_public=True, description=None
):
    p = Project(
        user_id=user.id,
        title=title,
        slug=slug,
        description=description,
        status=ProjectStatus.idea,
        is_public=is_public,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _load_fork_by_slug(db, user_id, slug):
    """Fetch a user's fork with relations + files eager-loaded for assertions."""
    result = await db.execute(
        select(Project)
        .options(
            selectinload(Project.updates),
            selectinload(Project.links),
            selectinload(Project.files).selectinload(ProjectFile.versions),
            selectinload(Project.files).selectinload(ProjectFile.current_version),
        )
        .where(Project.user_id == user_id, Project.slug == slug)
    )
    return result.scalar_one()


# ---------- route-level gates ---------- #


async def test_fork_requires_auth(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, slug="src", is_public=True)

    # No login: middleware redirects to /login for HTML POSTs, or returns
    # 401 when the dependency rejects. Accept either (the CSRF layer
    # currently surfaces 401 for unauthenticated POSTs).
    token = await csrf_token(client, "/login")
    resp = await client.post(f"/u/alice/{src.slug}/fork", data={"_csrf": token})
    assert resp.status_code in (401, 302, 303)

    assert (await db.execute(select(Project).where(Project.is_fork))).all() == []


async def test_fork_owner_self_fork_is_404(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, slug="src", is_public=True)

    await login(client, "alice")
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        f"/u/alice/{src.slug}/fork", data={"_csrf": token}
    )
    assert resp.status_code == 404


async def test_fork_private_source_is_404(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    src = await _make_project(db, alice, slug="priv", is_public=False)

    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        f"/u/alice/{src.slug}/fork", data={"_csrf": token}
    )
    assert resp.status_code == 404

    # No fork should have been created.
    assert (await db.execute(select(Project).where(Project.is_fork))).all() == []


async def test_fork_missing_project_is_404(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")

    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post("/u/alice/nope/fork", data={"_csrf": token})
    assert resp.status_code == 404


# ---------- happy path ---------- #


async def test_fork_creates_private_copy_owned_by_forker(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    src = await _make_project(
        db, alice, slug="src", is_public=True, description="Hello world"
    )

    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(
        f"/u/alice/{src.slug}/fork", data={"_csrf": token}
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/u/bob/{src.slug}"

    fork = await _load_fork_by_slug(db, bob.id, src.slug)
    assert fork.user_id == bob.id
    assert fork.title == src.title
    assert fork.description == "Hello world"
    assert fork.is_public is False
    assert fork.is_fork is True
    assert fork.forked_from_id == src.id
    assert fork.pinned is False


async def test_fork_copies_updates_links_files_and_versions(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    src = await _make_project(
        db, alice, slug="widget", is_public=True, description="Desc"
    )
    # Updates
    db.add(
        ProjectUpdate(
            project_id=src.id,
            title="First",
            content="entry one",
            is_public=True,
        )
    )
    db.add(
        ProjectUpdate(
            project_id=src.id,
            title=None,
            content="entry two",
            is_public=False,
        )
    )
    # Links
    db.add(
        ProjectLink(
            project_id=src.id,
            title="Source",
            url="https://example.test/foo",
            link_type=LinkType.github,
            sort_order=1,
        )
    )
    await db.commit()

    # Upload a file with two versions.
    await login(client, "alice")
    up1 = await _upload(
        client,
        f"/u/alice/{src.slug}/files",
        filename="notes.txt",
        content=b"v1 content",
        mime="text/plain",
        extra_form={"path": "docs"},
        csrf_path=f"/u/alice/{src.slug}",
    )
    assert up1.status_code == 302
    up2 = await _upload(
        client,
        f"/u/alice/{src.slug}/files",
        filename="notes.txt",
        content=b"v2 content is longer",
        mime="text/plain",
        extra_form={"path": "docs"},
        csrf_path=f"/u/alice/{src.slug}",
    )
    assert up2.status_code == 302

    # An image file too so we cover the thumbnail copy path.
    up_img = await _upload(
        client,
        f"/u/alice/{src.slug}/files",
        filename="cover.png",
        content=_png_bytes(100, 80),
        mime="image/png",
        csrf_path=f"/u/alice/{src.slug}",
    )
    assert up_img.status_code == 302

    # Log out alice, log in bob, fork.
    await client.post("/logout", data={"_csrf": await csrf_token(client, "/projects")})
    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(f"/u/alice/{src.slug}/fork", data={"_csrf": token})
    assert resp.status_code == 302

    fork = await _load_fork_by_slug(db, bob.id, src.slug)

    # Updates
    assert len(fork.updates) == 2
    titles = {u.title for u in fork.updates}
    assert titles == {"First", None}

    # Links
    assert len(fork.links) == 1
    assert fork.links[0].url == "https://example.test/foo"
    assert fork.links[0].link_type == LinkType.github

    # Files: two (notes.txt and cover.png).
    filenames = sorted(f.filename for f in fork.files)
    assert filenames == ["cover.png", "notes.txt"]

    # notes.txt has two versions copied.
    notes = next(f for f in fork.files if f.filename == "notes.txt")
    assert len(notes.versions) == 2
    v_numbers = sorted(v.version_number for v in notes.versions)
    assert v_numbers == [1, 2]
    # current_version points at the latest (v2).
    assert notes.current_version is not None
    assert notes.current_version.version_number == 2
    assert notes.current_version.size_bytes == len(b"v2 content is longer")

    # Blobs exist on disk at the new file's storage paths (independent of source).
    storage = get_storage()
    for v in notes.versions:
        assert storage.full_path(v.storage_path).exists()
        # New blob path references the NEW file id, not the source's id.
        assert v.storage_path.startswith(f"files/{notes.id}/")

    # Image file: thumbnail was copied along with the blob.
    cover = next(f for f in fork.files if f.filename == "cover.png")
    assert cover.current_version is not None
    assert cover.current_version.thumbnail_path is not None
    assert storage.full_path(cover.current_version.thumbnail_path).exists()

    # Total FileVersion count across all forked files.
    result = await db.execute(
        select(FileVersion)
        .join(ProjectFile, ProjectFile.id == FileVersion.file_id)
        .where(ProjectFile.project_id == fork.id)
    )
    fork_versions = list(result.scalars().all())
    assert len(fork_versions) == 3  # 2 notes + 1 cover


async def test_fork_creates_fork_of_relation(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    src = await _make_project(db, alice, slug="src", is_public=True)

    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(f"/u/alice/{src.slug}/fork", data={"_csrf": token})
    assert resp.status_code == 302

    fork = await _load_fork_by_slug(db, bob.id, src.slug)

    rows = (
        await db.execute(
            select(ProjectRelation).where(
                ProjectRelation.source_id == fork.id,
                ProjectRelation.target_id == src.id,
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].relation_type == RelationType.fork_of


async def test_fork_slug_deduped_in_forker_namespace(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    src = await _make_project(db, alice, slug="widget", is_public=True)
    # Bob already has a "widget" project of his own — the fork must not collide.
    await _make_project(db, bob, title="Mine", slug="widget", is_public=False)

    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(f"/u/alice/{src.slug}/fork", data={"_csrf": token})
    assert resp.status_code == 302
    # Dedup counter starts at 2 (matches unique_slug's pattern).
    assert resp.headers["location"] == "/u/bob/widget-2"

    # Verify both projects coexist.
    rows = (
        await db.execute(select(Project).where(Project.user_id == bob.id))
    ).scalars().all()
    slugs = {p.slug for p in rows}
    assert slugs == {"widget", "widget-2"}


# ---------- ancestry / deletion ---------- #


async def test_source_delete_sets_forked_from_to_null_but_keeps_is_fork(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    src = await _make_project(db, alice, slug="gone", is_public=True)

    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(f"/u/alice/{src.slug}/fork", data={"_csrf": token})
    assert resp.status_code == 302

    fork = await _load_fork_by_slug(db, bob.id, src.slug)
    assert fork.forked_from_id == src.id
    assert fork.is_fork is True

    # Delete the source.
    await db.delete(src)
    await db.commit()

    # Re-fetch the fork.
    db.expunge_all()
    refreshed = await _load_fork_by_slug(db, bob.id, src.slug)
    assert refreshed.forked_from_id is None
    assert refreshed.is_fork is True


# ---------- detail page rendering ---------- #


async def test_fork_detail_renders_forked_from_link(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    src = await _make_project(db, alice, slug="orig", is_public=True)

    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(f"/u/alice/{src.slug}/fork", data={"_csrf": token})
    assert resp.status_code == 302

    # Bob views his fork.
    detail = await client.get(f"/u/bob/{src.slug}")
    assert detail.status_code == 200
    assert "Forked from" in detail.text
    assert f'/u/alice/{src.slug}' in detail.text
    assert "@alice/orig" in detail.text


async def test_fork_detail_shows_deleted_project_when_parent_gone(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    src = await _make_project(db, alice, slug="orig", is_public=True)

    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(f"/u/alice/{src.slug}/fork", data={"_csrf": token})
    assert resp.status_code == 302

    # Delete the source. We need to log alice back in to POST a delete,
    # but since we're inside the test harness we can just delete via the
    # ORM for brevity.
    await db.delete(src)
    await db.commit()

    detail = await client.get(f"/u/bob/{src.slug}")
    assert detail.status_code == 200
    assert "Forked from a deleted project" in detail.text


# ---------- button visibility ---------- #


async def test_fork_button_visible_to_logged_in_non_owner_on_public_project(
    client, db
):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    src = await _make_project(db, alice, slug="shareme", is_public=True)

    await login(client, "bob")
    resp = await client.get(f"/u/alice/{src.slug}")
    assert resp.status_code == 200
    # Button is a form posting to /u/alice/shareme/fork.
    assert f'action="/u/alice/{src.slug}/fork"' in resp.text


async def test_fork_button_not_visible_to_owner(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, slug="mine", is_public=True)

    await login(client, "alice")
    resp = await client.get(f"/u/alice/{src.slug}")
    assert resp.status_code == 200
    assert f'action="/u/alice/{src.slug}/fork"' not in resp.text


async def test_fork_button_not_visible_to_guest(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    src = await _make_project(db, alice, slug="shareme", is_public=True)

    resp = await client.get(f"/u/alice/{src.slug}")
    assert resp.status_code == 200
    assert f'action="/u/alice/{src.slug}/fork"' not in resp.text


async def test_fork_button_not_visible_on_private_project(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    # Private → a non-owner can't see the page at all, but even with the
    # owner viewing (or if visibility flipped later) the button shouldn't
    # appear. Easiest check: owner's private project has no Fork button.
    src = await _make_project(db, alice, slug="priv", is_public=False)

    await login(client, "alice")
    resp = await client.get(f"/u/alice/{src.slug}")
    assert resp.status_code == 200
    assert f'action="/u/alice/{src.slug}/fork"' not in resp.text
