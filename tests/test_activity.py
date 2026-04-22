"""Tests for the Activity feed — write path, visibility, and rendering
across project/profile/explore surfaces.

Covers every event type firing once (and not on edit), visibility filters
at the list-helper level, and smoke-level template rendering of each
event type in each of the three feed surfaces.
"""

import functools
import io
import shutil

import pytest
from PIL import Image
from sqlalchemy import select

from benchlog.activity import (
    list_global_activity,
    list_project_activity,
    list_user_activity,
)
from benchlog.config import settings
from benchlog.models import (
    ActivityEvent,
    ActivityEventType,
    JournalEntry,
    Project,
    ProjectLink,
    ProjectStatus,
)
from benchlog.storage import get_storage
from tests.conftest import csrf_token, login, make_user, post_form


# ---------- fixtures / helpers ---------- #


@pytest.fixture(autouse=True)
def _isolate_storage(tmp_path, monkeypatch):
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


async def _events(db, *, project_id=None, actor_id=None):
    q = select(ActivityEvent)
    if project_id is not None:
        q = q.where(ActivityEvent.project_id == project_id)
    if actor_id is not None:
        q = q.where(ActivityEvent.actor_id == actor_id)
    q = q.order_by(ActivityEvent.created_at)
    return list((await db.execute(q)).scalars().all())


async def _project(db, user, *, title="Bench", slug="bench", is_public=False):
    p = Project(
        user_id=user.id,
        title=title,
        slug=slug,
        status=ProjectStatus.idea,
        is_public=is_public,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


# ---------- write path: project_created / visibility flips ---------- #


async def test_create_project_records_event(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")
    resp = await post_form(
        client,
        "/projects",
        {
            "title": "First",
            "status": ProjectStatus.idea.value,
        },
        csrf_path="/projects",
    )
    assert resp.status_code == 302

    events = (await db.execute(select(ActivityEvent))).scalars().all()
    assert len(events) == 1
    assert events[0].event_type == ActivityEventType.project_created


async def test_edit_project_without_visibility_flip_records_nothing(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench", is_public=False)

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"title": "Renamed"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 204

    events = await _events(db, project_id=project.id)
    assert events == []


async def test_flip_to_public_records_no_event(client, db):
    # Visibility flips are intentionally silent — a flip to public should
    # leave the activity log alone.
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench", is_public=False)

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"is_public": "1"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 204

    events = await _events(db, project_id=project.id)
    assert events == []


async def test_flip_to_private_records_nothing(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench", is_public=True)

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"is_public": "0"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 204

    events = await _events(db, project_id=project.id)
    assert events == []


async def test_public_toggle_true_false_true_records_nothing(client, db):
    # Visibility flips are silent — cycling on/off/on should produce no
    # activity events at all.
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench", is_public=False)

    await login(client, "alice")
    # Off -> On
    await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"is_public": "1"},
        csrf_path="/projects/new",
    )
    # On -> Off
    await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"is_public": "0"},
        csrf_path="/projects/new",
    )
    # Off -> On again
    await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"is_public": "1"},
        csrf_path="/projects/new",
    )

    events = await _events(db, project_id=project.id)
    assert events == []


# ---------- write path: project_forked ---------- #


async def test_fork_records_event_on_new_project(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    src = await _project(db, alice, slug="src", is_public=True)

    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(f"/u/alice/{src.slug}/fork", data={"_csrf": token})
    assert resp.status_code == 302

    # Fork belongs to bob. The project_forked event sits on the NEW project,
    # not the source.
    new_project = (
        await db.execute(
            select(Project).where(
                Project.user_id == bob.id, Project.slug == src.slug
            )
        )
    ).scalar_one()

    src_events = await _events(db, project_id=src.id)
    assert src_events == []

    fork_events = await _events(db, project_id=new_project.id)
    assert [e.event_type for e in fork_events] == [
        ActivityEventType.project_forked
    ]
    assert fork_events[0].payload == {"source_project_id": str(src.id)}
    assert fork_events[0].actor_id == bob.id


async def test_forked_event_survives_source_deletion(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    src = await _project(db, alice, slug="orig", is_public=True)

    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post(f"/u/alice/{src.slug}/fork", data={"_csrf": token})
    assert resp.status_code == 302

    new_project = (
        await db.execute(
            select(Project).where(
                Project.user_id == bob.id, Project.slug == src.slug
            )
        )
    ).scalar_one()
    stale_source_id = str(src.id)

    await db.delete(src)
    await db.commit()

    # The fork's event should still be there, still pointing at the now-stale source id.
    events = await _events(db, project_id=new_project.id)
    assert len(events) == 1
    assert events[0].payload["source_project_id"] == stale_source_id

    # Render the global feed to confirm the stale ref doesn't crash — this
    # project is private (forks default private) so no events should surface.
    fetched = await list_global_activity(db, viewer_id=None)
    assert all(e.project_id != new_project.id for e in fetched)


# ---------- write path: journal ---------- #


async def test_titled_journal_entry_records_event(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench")

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/journal",
        {"title": "Day 1", "content": "Glued."},
        csrf_path=f"/u/alice/{project.slug}",
    )
    assert resp.status_code == 302

    events = await _events(db, project_id=project.id)
    assert [e.event_type for e in events] == [
        ActivityEventType.journal_entry_posted
    ]
    assert "entry_id" in events[0].payload


async def test_untitled_journal_entry_also_records_event(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench")

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/journal",
        {"title": "", "content": "Just noting something."},
        csrf_path=f"/u/alice/{project.slug}",
    )
    assert resp.status_code == 302

    events = await _events(db, project_id=project.id)
    assert [e.event_type for e in events] == [
        ActivityEventType.journal_entry_posted
    ]


async def test_journal_edit_records_no_new_event(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench")

    await login(client, "alice")
    # Create
    await post_form(
        client,
        f"/u/alice/{project.slug}/journal",
        {"title": "Day 1", "content": "Original."},
        csrf_path=f"/u/alice/{project.slug}",
    )
    events_before = await _events(db, project_id=project.id)
    assert len(events_before) == 1

    # Edit
    await post_form(
        client,
        f"/u/alice/{project.slug}/journal/day-1",
        {"title": "Day 1", "content": "Edited."},
        csrf_path=f"/u/alice/{project.slug}",
    )
    events_after = await _events(db, project_id=project.id)
    assert len(events_after) == 1


# ---------- write path: files ---------- #


async def test_upload_new_file_records_file_uploaded(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench")

    await login(client, "alice")
    resp = await _upload(
        client,
        f"/u/alice/{project.slug}/files",
        filename="notes.txt",
        content=b"hello",
        mime="text/plain",
        csrf_path=f"/u/alice/{project.slug}",
    )
    assert resp.status_code == 302

    events = await _events(db, project_id=project.id)
    assert [e.event_type for e in events] == [ActivityEventType.file_uploaded]
    assert events[0].payload["filename"] == "notes.txt"
    assert "file_id" in events[0].payload


async def test_reupload_same_path_records_version_added(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench")

    await login(client, "alice")
    # First upload -> file_uploaded
    await _upload(
        client,
        f"/u/alice/{project.slug}/files",
        filename="notes.txt",
        content=b"v1",
        mime="text/plain",
        csrf_path=f"/u/alice/{project.slug}",
    )
    # Second upload same (path, filename) -> file_version_added
    await _upload(
        client,
        f"/u/alice/{project.slug}/files",
        filename="notes.txt",
        content=b"v2 longer",
        mime="text/plain",
        csrf_path=f"/u/alice/{project.slug}",
    )

    events = await _events(db, project_id=project.id)
    types = [e.event_type for e in events]
    assert types == [
        ActivityEventType.file_uploaded,
        ActivityEventType.file_version_added,
    ]
    assert events[1].payload["version_number"] == 2


async def test_deleting_file_purges_its_events(client, db):
    # Upload + reupload gives us one file_uploaded + one file_version_added
    # sharing the same file_id. Deleting the file should clear both.
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench")

    await login(client, "alice")
    await _upload(
        client,
        f"/u/alice/{project.slug}/files",
        filename="notes.txt",
        content=b"v1",
        mime="text/plain",
        csrf_path=f"/u/alice/{project.slug}",
    )
    await _upload(
        client,
        f"/u/alice/{project.slug}/files",
        filename="notes.txt",
        content=b"v2 longer",
        mime="text/plain",
        csrf_path=f"/u/alice/{project.slug}",
    )
    events = await _events(db, project_id=project.id)
    assert len(events) == 2
    file_id = events[0].payload["file_id"]

    await post_form(
        client,
        f"/u/alice/{project.slug}/files/{file_id}/delete",
        csrf_path=f"/u/alice/{project.slug}",
    )

    events = await _events(db, project_id=project.id)
    file_events = [
        e for e in events
        if e.event_type in (
            ActivityEventType.file_uploaded,
            ActivityEventType.file_version_added,
        )
    ]
    assert file_events == []


async def test_deleting_file_version_purges_only_that_version_event(client, db):
    # Three versions → one file_uploaded + two file_version_added. Deleting
    # version 2 should remove exactly the matching file_version_added row.
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench")

    await login(client, "alice")
    for payload in (b"v1", b"v2 longer", b"v3 longer still"):
        await _upload(
            client,
            f"/u/alice/{project.slug}/files",
            filename="notes.txt",
            content=payload,
            mime="text/plain",
            csrf_path=f"/u/alice/{project.slug}",
        )
    events = await _events(db, project_id=project.id)
    file_id = events[0].payload["file_id"]
    assert [e.payload.get("version_number") for e in events if e.payload.get("version_number")] == [2, 3]

    await post_form(
        client,
        f"/u/alice/{project.slug}/files/{file_id}/version/2/delete",
        csrf_path=f"/u/alice/{project.slug}",
    )

    events = await _events(db, project_id=project.id)
    version_numbers = sorted(
        e.payload["version_number"] for e in events
        if e.event_type == ActivityEventType.file_version_added
    )
    assert version_numbers == [3]


# ---------- write path: links ---------- #


async def test_create_link_records_link_added(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench")

    await login(client, "alice")
    token = await csrf_token(client, f"/u/alice/{project.slug}")
    resp = await client.post(
        f"/u/alice/{project.slug}/links",
        data={
            "_csrf": token,
            "title": "Upstream spec",
            "url": "https://example.com/spec",
            "link_type": "other",
        },
    )
    assert resp.status_code == 302

    link = (
        await db.execute(
            select(ProjectLink).where(ProjectLink.project_id == project.id)
        )
    ).scalar_one()

    events = await _events(db, project_id=project.id)
    assert [e.event_type for e in events] == [ActivityEventType.link_added]
    payload = events[0].payload
    assert payload["link_id"] == str(link.id)
    assert payload["label"] == "Upstream spec"
    assert payload["url"] == "https://example.com/spec"


async def test_delete_link_records_link_removed_and_preserves_link_added(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench")

    await login(client, "alice")
    token = await csrf_token(client, f"/u/alice/{project.slug}")
    await client.post(
        f"/u/alice/{project.slug}/links",
        data={
            "_csrf": token,
            "title": "Upstream spec",
            "url": "https://example.com/spec",
            "link_type": "other",
        },
    )
    link = (
        await db.execute(
            select(ProjectLink).where(ProjectLink.project_id == project.id)
        )
    ).scalar_one()
    link_id = link.id

    await post_form(
        client,
        f"/u/alice/{project.slug}/links/{link_id}/delete",
        csrf_path=f"/u/alice/{project.slug}",
    )

    events = await _events(db, project_id=project.id)
    assert [e.event_type for e in events] == [
        ActivityEventType.link_added,
        ActivityEventType.link_removed,
    ]
    # The earlier link_added row is intentionally NOT purged — the payload
    # just orphans its link_id.
    added = events[0]
    assert added.payload["link_id"] == str(link_id)

    removed = events[1]
    assert removed.payload == {
        "label": "Upstream spec",
        "url": "https://example.com/spec",
    }


async def test_edit_link_records_no_event(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench")

    await login(client, "alice")
    token = await csrf_token(client, f"/u/alice/{project.slug}")
    await client.post(
        f"/u/alice/{project.slug}/links",
        data={
            "_csrf": token,
            "title": "Upstream",
            "url": "https://example.com/spec",
            "link_type": "other",
        },
    )
    link = (
        await db.execute(
            select(ProjectLink).where(ProjectLink.project_id == project.id)
        )
    ).scalar_one()

    events_before = await _events(db, project_id=project.id)
    assert [e.event_type for e in events_before] == [ActivityEventType.link_added]

    # Edit the link — mirrors journal-edit's convention of not emitting.
    token = await csrf_token(client, f"/u/alice/{project.slug}")
    resp = await client.post(
        f"/u/alice/{project.slug}/links/{link.id}",
        data={
            "_csrf": token,
            "title": "Upstream v2",
            "url": "https://example.com/spec-v2",
            "link_type": "other",
        },
    )
    assert resp.status_code == 302

    events_after = await _events(db, project_id=project.id)
    assert [e.event_type for e in events_after] == [ActivityEventType.link_added]


async def test_link_events_follow_project_visibility(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    pub = await _project(db, alice, slug="pub", is_public=True)
    priv = await _project(db, alice, slug="priv", is_public=False)

    await login(client, "alice")
    for slug in (pub.slug, priv.slug):
        token = await csrf_token(client, f"/u/alice/{slug}")
        await client.post(
            f"/u/alice/{slug}/links",
            data={
                "_csrf": token,
                "title": "Upstream",
                "url": f"https://example.com/{slug}",
                "link_type": "other",
            },
        )

    # Guest global firehose: public only.
    events = await list_global_activity(db, viewer_id=None)
    project_ids = {e.project_id for e in events if e.event_type == ActivityEventType.link_added}
    assert pub.id in project_ids
    assert priv.id not in project_ids

    # Third-party profile view: same rule.
    bob = await make_user(db, email="bob@test.com", username="bob")
    events = await list_user_activity(db, alice.id, viewer_id=bob.id)
    project_ids = {e.project_id for e in events if e.event_type == ActivityEventType.link_added}
    assert pub.id in project_ids
    assert priv.id not in project_ids

    # Owner sees both on her own profile.
    events = await list_user_activity(db, alice.id, viewer_id=alice.id)
    project_ids = {e.project_id for e in events if e.event_type == ActivityEventType.link_added}
    assert pub.id in project_ids
    assert priv.id in project_ids


# ---------- visibility: list_user_activity ---------- #


async def test_list_user_activity_guest_sees_public_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    # Private project event
    priv = await _project(db, alice, slug="priv", is_public=False)
    # Public project event
    pub = await _project(db, alice, slug="pub", is_public=True)

    # Seed events directly via record_event through routes to populate both.
    await login(client, "alice")
    # Entries marked public so this test isolates project-level visibility
    # from the per-entry visibility filter.
    await post_form(
        client,
        f"/u/alice/{priv.slug}/journal",
        {"title": "", "content": "private note", "is_public": "on"},
        csrf_path=f"/u/alice/{priv.slug}",
    )
    await post_form(
        client,
        f"/u/alice/{pub.slug}/journal",
        {"title": "", "content": "public note", "is_public": "on"},
        csrf_path=f"/u/alice/{pub.slug}",
    )

    # Guest (viewer_id=None): public only.
    events = await list_user_activity(db, alice.id, viewer_id=None)
    assert len(events) == 1
    assert events[0].project_id == pub.id

    # Owner viewing own: sees both.
    events = await list_user_activity(db, alice.id, viewer_id=alice.id)
    assert len(events) == 2

    # Third-party logged-in viewer: sees public only.
    bob = await make_user(db, email="bob@test.com", username="bob")
    events = await list_user_activity(db, alice.id, viewer_id=bob.id)
    assert len(events) == 1
    assert events[0].project_id == pub.id


# ---------- visibility: list_global_activity ---------- #


async def test_global_activity_public_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    priv = await _project(db, alice, slug="priv", is_public=False)
    pub = await _project(db, alice, slug="pub", is_public=True)

    await login(client, "alice")
    # Entries marked public so this test isolates project-level visibility
    # from the per-entry visibility filter.
    await post_form(
        client,
        f"/u/alice/{priv.slug}/journal",
        {"title": "", "content": "private", "is_public": "on"},
        csrf_path=f"/u/alice/{priv.slug}",
    )
    await post_form(
        client,
        f"/u/alice/{pub.slug}/journal",
        {"title": "", "content": "public", "is_public": "on"},
        csrf_path=f"/u/alice/{pub.slug}",
    )

    events = await list_global_activity(db, viewer_id=None)
    project_ids = {e.project_id for e in events}
    assert pub.id in project_ids
    assert priv.id not in project_ids


async def test_deleting_journal_entry_purges_its_event(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench", is_public=True)

    await login(client, "alice")
    await post_form(
        client,
        f"/u/alice/{project.slug}/journal",
        {"title": "Temp", "content": "x", "is_public": "on"},
        csrf_path=f"/u/alice/{project.slug}",
    )
    entry = (
        await db.execute(select(JournalEntry).where(JournalEntry.project_id == project.id))
    ).scalar_one()
    events = await list_project_activity(db, project.id, viewer_id=alice.id)
    assert any(
        e.event_type == ActivityEventType.journal_entry_posted for e in events
    )

    await post_form(
        client,
        f"/u/alice/{project.slug}/journal/{entry.slug}/delete",
        csrf_path=f"/u/alice/{project.slug}",
    )

    events = await list_project_activity(db, project.id, viewer_id=alice.id)
    assert not any(
        e.event_type == ActivityEventType.journal_entry_posted for e in events
    )


async def test_events_disappear_from_public_feeds_when_project_flips_private(client, db):
    # Visibility reflects current is_public — no historical snapshot.
    # A project's events leave the guest/global feeds when it goes private
    # and return if it flips public again.
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench", is_public=True)

    await login(client, "alice")
    await post_form(
        client,
        f"/u/alice/{project.slug}/journal",
        {"title": "Visible", "content": "x", "is_public": "on"},
        csrf_path=f"/u/alice/{project.slug}",
    )

    events = await list_global_activity(db, viewer_id=None)
    assert any(e.project_id == project.id for e in events)

    project.is_public = False
    await db.commit()

    events = await list_global_activity(db, viewer_id=None)
    assert not any(e.project_id == project.id for e in events)

    project.is_public = True
    await db.commit()

    events = await list_global_activity(db, viewer_id=None)
    assert any(e.project_id == project.id for e in events)


async def test_private_journal_entry_hidden_from_non_actor_viewers(client, db):
    # A private journal entry on a public project: the owner sees the
    # event in any feed, but non-owner viewers shouldn't — posting a
    # private entry shouldn't leak its existence via activity.
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    project = await _project(db, alice, slug="bench", is_public=True)

    await login(client, "alice")
    # Public entry — surfaces to everyone.
    await post_form(
        client,
        f"/u/alice/{project.slug}/journal",
        {"title": "Public one", "content": "p", "is_public": "on"},
        csrf_path=f"/u/alice/{project.slug}",
    )
    # Private entry — should be hidden from non-actor viewers.
    await post_form(
        client,
        f"/u/alice/{project.slug}/journal",
        {"title": "Private one", "content": "s"},
        csrf_path=f"/u/alice/{project.slug}",
    )

    # Alice (the actor) sees both.
    events = await list_project_activity(db, project.id, viewer_id=alice.id)
    journal_events = [
        e for e in events
        if e.event_type == ActivityEventType.journal_entry_posted
    ]
    assert len(journal_events) == 2

    # Bob (non-actor) sees only the public entry's event.
    events = await list_project_activity(db, project.id, viewer_id=bob.id)
    journal_events = [
        e for e in events
        if e.event_type == ActivityEventType.journal_entry_posted
    ]
    assert len(journal_events) == 1

    # Guest — same, public only.
    events = await list_project_activity(db, project.id, viewer_id=None)
    journal_events = [
        e for e in events
        if e.event_type == ActivityEventType.journal_entry_posted
    ]
    assert len(journal_events) == 1


async def test_private_events_not_on_explore_activity(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    priv = await _project(db, alice, slug="priv", is_public=False)

    await login(client, "alice")
    await post_form(
        client,
        f"/u/alice/{priv.slug}/journal",
        {"title": "Secret", "content": "Shh"},
        csrf_path=f"/u/alice/{priv.slug}",
    )
    # Log out, visit as guest.
    await client.post(
        "/logout", data={"_csrf": await csrf_token(client, "/explore")}
    )
    resp = await client.get("/explore/activity")
    assert resp.status_code == 200
    # Neither the slug nor the word "Secret" should appear in the public feed.
    assert "priv" not in resp.text or "/u/alice/priv" not in resp.text


async def test_private_events_not_on_third_party_profile(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    priv = await _project(db, alice, slug="priv", is_public=False)

    await login(client, "alice")
    await post_form(
        client,
        f"/u/alice/{priv.slug}/journal",
        {"title": "", "content": "private"},
        csrf_path=f"/u/alice/{priv.slug}",
    )
    # A third-party user views Alice's profile.
    await client.post(
        "/logout", data={"_csrf": await csrf_token(client, "/explore")}
    )
    await make_user(db, email="bob@test.com", username="bob")
    await login(client, "bob")

    resp = await client.get("/u/alice")
    assert resp.status_code == 200
    # Private-project journal URL must not leak.
    assert "/u/alice/priv" not in resp.text


# ---------- rendering: smoke tests ---------- #


async def _seed_every_event_type(client, db, alice, bob):
    """Fire one of each emitted event type into the shared project.

    - project_created: from the create route
    - journal_entry_posted: post an entry
    - file_uploaded: upload a file
    - file_version_added: re-upload same path
    - link_added / link_removed: create + delete a project link
    - project_forked: bob forks the (now public) project

    Visibility flips don't emit any event; we still flip to public here so
    the fork + global firehose assertions work.
    """
    await login(client, "alice")
    await post_form(
        client,
        "/projects",
        {"title": "Shared", "slug": "shared", "status": ProjectStatus.idea.value},
        csrf_path="/projects",
    )
    # Flip to public (no event emitted on this, just needed for visibility).
    await post_form(
        client,
        "/u/alice/shared/settings",
        {"is_public": "1"},
        csrf_path="/projects/new",
    )
    # Journal
    await post_form(
        client,
        "/u/alice/shared/journal",
        {"title": "Entry", "content": "Hello", "is_public": "on"},
        csrf_path="/u/alice/shared",
    )
    # File + version
    await _upload(
        client,
        "/u/alice/shared/files",
        filename="notes.txt",
        content=b"v1",
        mime="text/plain",
        csrf_path="/u/alice/shared",
    )
    await _upload(
        client,
        "/u/alice/shared/files",
        filename="notes.txt",
        content=b"v2 more",
        mime="text/plain",
        csrf_path="/u/alice/shared",
    )
    # Link add + remove
    token = await csrf_token(client, "/u/alice/shared")
    await client.post(
        "/u/alice/shared/links",
        data={
            "_csrf": token,
            "title": "Upstream",
            "url": "https://example.com/spec",
            "link_type": "other",
        },
    )
    link = (
        await db.execute(
            select(ProjectLink).where(ProjectLink.url == "https://example.com/spec")
        )
    ).scalar_one()
    await post_form(
        client,
        f"/u/alice/shared/links/{link.id}/delete",
        csrf_path="/u/alice/shared",
    )
    # Logout; bob forks
    await client.post(
        "/logout", data={"_csrf": await csrf_token(client, "/explore")}
    )
    await login(client, "bob")
    token = await csrf_token(client, "/projects")
    resp = await client.post("/u/alice/shared/fork", data={"_csrf": token})
    assert resp.status_code == 302
    # Bob makes his fork public so the global firehose lights up.
    await post_form(
        client,
        "/u/bob/shared/settings",
        {"is_public": "1"},
        csrf_path="/projects/new",
    )


async def test_all_three_pages_render_with_every_event_type(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    await _seed_every_event_type(client, db, alice, bob)

    # Per-project activity tab.
    resp = await client.get("/u/alice/shared/activity")
    assert resp.status_code == 200
    assert "Activity" in resp.text

    # Profile page: alice's recent activity section.
    resp = await client.get("/u/alice")
    assert resp.status_code == 200
    assert "Recent activity" in resp.text

    # Global firehose.
    resp = await client.get("/explore/activity")
    assert resp.status_code == 200


# ---------- tab visibility ---------- #


async def test_activity_tab_visible_to_owner(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench", is_public=False)

    await login(client, "alice")
    resp = await client.get(f"/u/alice/{project.slug}")
    assert resp.status_code == 200
    assert f"/u/alice/{project.slug}/activity" in resp.text


async def test_activity_tab_visible_to_guest_on_public(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench", is_public=True)

    resp = await client.get(f"/u/alice/{project.slug}")
    assert resp.status_code == 200
    assert f"/u/alice/{project.slug}/activity" in resp.text


async def test_activity_tab_404_for_guest_on_private(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench", is_public=False)

    resp = await client.get(f"/u/alice/{project.slug}/activity")
    assert resp.status_code == 404


# ---------- per-user activity dashboard ---------- #


async def test_profile_activity_page_200_for_existing_user(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench", is_public=True)

    await login(client, "alice")
    await post_form(
        client,
        f"/u/alice/{project.slug}/journal",
        {"title": "Hi", "content": "x", "is_public": "on"},
        csrf_path=f"/u/alice/{project.slug}",
    )

    resp = await client.get("/u/alice/activity")
    assert resp.status_code == 200
    assert "Activity" in resp.text


async def test_profile_activity_page_404_for_unknown_user(client):
    resp = await client.get("/u/nope/activity")
    assert resp.status_code == 404


async def test_profile_activity_guest_sees_public_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    pub = await _project(db, alice, slug="pub", is_public=True)
    priv = await _project(db, alice, slug="priv", is_public=False)

    await login(client, "alice")
    await post_form(
        client,
        f"/u/alice/{pub.slug}/journal",
        {"title": "Open", "content": "p", "is_public": "on"},
        csrf_path=f"/u/alice/{pub.slug}",
    )
    await post_form(
        client,
        f"/u/alice/{priv.slug}/journal",
        {"title": "Hidden", "content": "s", "is_public": "on"},
        csrf_path=f"/u/alice/{priv.slug}",
    )

    # Log out; read as guest.
    await client.post(
        "/logout", data={"_csrf": await csrf_token(client, "/explore")}
    )
    resp = await client.get("/u/alice/activity")
    assert resp.status_code == 200
    assert "/u/alice/pub" in resp.text
    assert "/u/alice/priv" not in resp.text


async def test_profile_activity_owner_sees_private(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    priv = await _project(db, alice, slug="priv", is_public=False)

    await login(client, "alice")
    await post_form(
        client,
        f"/u/alice/{priv.slug}/journal",
        {"title": "Hidden", "content": "s"},
        csrf_path=f"/u/alice/{priv.slug}",
    )

    resp = await client.get("/u/alice/activity")
    assert resp.status_code == 200
    assert "/u/alice/priv" in resp.text


async def test_profile_activity_third_party_public_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    pub = await _project(db, alice, slug="pub", is_public=True)
    priv = await _project(db, alice, slug="priv", is_public=False)

    await login(client, "alice")
    await post_form(
        client,
        f"/u/alice/{pub.slug}/journal",
        {"title": "Open", "content": "p", "is_public": "on"},
        csrf_path=f"/u/alice/{pub.slug}",
    )
    await post_form(
        client,
        f"/u/alice/{priv.slug}/journal",
        {"title": "Hidden", "content": "s"},
        csrf_path=f"/u/alice/{priv.slug}",
    )

    # Log out; log in as a third party.
    await client.post(
        "/logout", data={"_csrf": await csrf_token(client, "/explore")}
    )
    await make_user(db, email="bob@test.com", username="bob")
    await login(client, "bob")

    resp = await client.get("/u/alice/activity")
    assert resp.status_code == 200
    assert "/u/alice/pub" in resp.text
    assert "/u/alice/priv" not in resp.text


async def test_profile_activity_pagination(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench", is_public=True)

    # Seed 55 synthetic events directly (actor=alice, project=public).
    for _ in range(55):
        db.add(
            ActivityEvent(
                actor_id=alice.id,
                project_id=project.id,
                event_type=ActivityEventType.project_created,
            )
        )
    await db.commit()

    # Page 1: 50 events + "Load older" link pointing at offset=50.
    resp = await client.get("/u/alice/activity")
    assert resp.status_code == 200
    assert "/u/alice/activity?offset=50" in resp.text

    # Page 2: the remaining 5 events, no "Load older" link.
    resp = await client.get("/u/alice/activity?offset=50")
    assert resp.status_code == 200
    assert "/u/alice/activity?offset=100" not in resp.text


async def test_profile_shows_view_all_link_when_activity_present(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench", is_public=True)

    await login(client, "alice")
    await post_form(
        client,
        f"/u/alice/{project.slug}/journal",
        {"title": "Hi", "content": "x", "is_public": "on"},
        csrf_path=f"/u/alice/{project.slug}",
    )

    resp = await client.get("/u/alice")
    assert resp.status_code == 200
    assert "/u/alice/activity" in resp.text


async def test_profile_activity_inactive_user_404(client, db):
    await make_user(
        db, email="ghost@test.com", username="ghost", is_active=False
    )
    resp = await client.get("/u/ghost/activity")
    assert resp.status_code == 404


async def test_project_activity_returns_in_recency_order(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    project = await _project(db, alice, slug="bench", is_public=True)

    await login(client, "alice")
    await post_form(
        client,
        f"/u/alice/{project.slug}/journal",
        {"title": "One", "content": "A"},
        csrf_path=f"/u/alice/{project.slug}",
    )
    await post_form(
        client,
        f"/u/alice/{project.slug}/journal",
        {"title": "Two", "content": "B"},
        csrf_path=f"/u/alice/{project.slug}",
    )

    events = await list_project_activity(db, project.id, viewer_id=alice.id)
    # Two journal events, newest first.
    assert len(events) == 2
    assert events[0].created_at >= events[1].created_at
