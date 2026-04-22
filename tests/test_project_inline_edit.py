"""Tests for the inline project-detail edit surfaces.

Covers:
  - `POST /u/{user}/{slug}/settings` — the workhorse endpoint behind every
    chip / title / tag / category / slug edit on the detail page.
  - Owner-vs-non-owner rendering of the header (status chip dropdown,
    public/pinned chips, inline title edit, slug + delete in More actions).
  - `project_became_public` event fires on False → True transitions.
"""

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from benchlog.models import (
    ActivityEvent,
    ActivityEventType,
    Project,
    ProjectStatus,
)
from tests.conftest import csrf_token, login, make_user, post_form


async def _events(db, *, project_id):
    result = await db.execute(
        select(ActivityEvent)
        .where(ActivityEvent.project_id == project_id)
        .order_by(ActivityEvent.created_at.asc())
    )
    return list(result.scalars().all())


# ---------- settings endpoint: auth gates ---------- #


async def test_settings_requires_login(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=alice.id,
            title="P",
            slug="p",
            status=ProjectStatus.idea,
            is_public=True,
        )
    )
    await db.commit()

    # No session — CSRF middleware runs first but unauthenticated POSTs to
    # private endpoints are bounced by the auth middleware either way.
    resp = await client.post("/u/alice/p/settings", data={"title": "x"})
    # 403 from CSRF (no token) — middleware order guarantees this; if the
    # whole path ever became auth-gated earlier we'd see 302 instead.
    assert resp.status_code in (302, 403)


async def test_settings_non_owner_gets_404(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    db.add(
        Project(
            user_id=alice.id,
            title="P",
            slug="p",
            status=ProjectStatus.idea,
            is_public=True,
        )
    )
    await db.commit()

    await login(client, "bob")
    resp = await post_form(
        client,
        "/u/alice/p/settings",
        {"title": "hijack"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 404


async def test_settings_username_mismatch_is_404(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=alice.id,
            title="P",
            slug="p",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")
    # alice logged in, but hitting the route under bob's namespace is a 404
    # the same way GET /u/bob/p would be.
    resp = await post_form(
        client,
        "/u/bob/p/settings",
        {"title": "x"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 404


# ---------- settings endpoint: partial-update semantics ---------- #


async def test_settings_missing_fields_are_unchanged(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Original",
        slug="orig",
        status=ProjectStatus.in_progress,
        pinned=True,
        is_public=True,
    )
    db.add(project)
    await db.commit()

    await login(client, "alice")
    # Submit a body with nothing except the csrf token.
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 204
    await db.refresh(project)
    assert project.title == "Original"
    assert project.status == ProjectStatus.in_progress
    assert project.pinned is True
    assert project.is_public is True


async def test_settings_title_update(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Before",
        slug="x",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"title": "After"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 204
    await db.refresh(project)
    assert project.title == "After"


async def test_settings_empty_title_is_400(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Keep",
        slug="k",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"title": "   "},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "Title is required." in body["detail"]
    await db.refresh(project)
    assert project.title == "Keep"


async def test_settings_status_update(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="P",
        slug="p",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"status": "completed"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 204
    await db.refresh(project)
    assert project.status == ProjectStatus.completed


async def test_settings_unknown_status_is_400(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="P",
        slug="p",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"status": "bogus"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 400


async def test_settings_pinned_toggle(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="P",
        slug="p",
        status=ProjectStatus.idea,
        pinned=False,
    )
    db.add(project)
    await db.commit()

    await login(client, "alice")
    # True
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"pinned": "1"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 204
    await db.refresh(project)
    assert project.pinned is True

    # False — explicit "0" must turn it off (the old form used absence).
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"pinned": "0"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 204
    await db.refresh(project)
    assert project.pinned is False


async def test_settings_is_public_flip_records_event(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="P",
        slug="p",
        status=ProjectStatus.idea,
        is_public=False,
    )
    db.add(project)
    await db.commit()

    await login(client, "alice")
    # False -> True
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"is_public": "1"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 204
    await db.refresh(project)
    assert project.is_public is True
    events = await _events(db, project_id=project.id)
    assert [e.event_type for e in events] == [
        ActivityEventType.project_became_public
    ]

    # True -> True (no transition) — no new event.
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"is_public": "1"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 204
    events = await _events(db, project_id=project.id)
    assert len(events) == 1

    # True -> False — no event.
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"is_public": "0"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 204
    events = await _events(db, project_id=project.id)
    assert len(events) == 1


async def test_settings_slug_change_returns_redirect_json(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="P",
        slug="oldslug",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"slug": "newslug"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"redirect": "/u/alice/newslug"}
    await db.refresh(project)
    assert project.slug == "newslug"


async def test_settings_slug_same_as_current_is_no_op(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="P",
        slug="keepme",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()

    await login(client, "alice")
    # Sending the same slug back should save cleanly with a 204 (no
    # redirect — URL stays the same).
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"slug": "keepme"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 204


async def test_settings_slug_collision_is_400(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add_all(
        [
            Project(
                user_id=user.id,
                title="A",
                slug="taken",
                status=ProjectStatus.idea,
            ),
            Project(
                user_id=user.id,
                title="B",
                slug="free",
                status=ProjectStatus.idea,
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        "/u/alice/free/settings",
        {"slug": "taken"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "already used" in body["detail"]


async def test_settings_empty_slug_is_400(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="P",
            slug="p",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        "/u/alice/p/settings",
        {"slug": "!!!"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 400


async def test_settings_tags_replace(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")
    await post_form(
        client,
        "/projects",
        {"title": "P", "status": "idea", "tags": "foo, bar"},
        csrf_path="/projects/new",
    )
    project = (
        await db.execute(
            select(Project).options(selectinload(Project.tags)).where(Project.user_id == user.id)
        )
    ).scalar_one()

    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"tags": "baz, qux"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 204
    await db.refresh(project, ["tags"])
    assert sorted(t.slug for t in project.tags) == ["baz", "qux"]


async def test_settings_categories_replace(client, db):
    from benchlog.categories import set_project_categories
    from benchlog.models import Category

    a = Category(slug="a", name="A")
    b = Category(slug="b", name="B")
    c = Category(slug="c", name="C")
    db.add_all([a, b, c])
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="P",
        slug="p",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    await set_project_categories(db, project, [str(a.id), str(b.id)])
    await db.commit()

    await login(client, "alice")
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"categories": [str(b.id), str(c.id)]},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 204
    db.expunge_all()
    refreshed = (
        await db.execute(
            select(Project)
            .options(selectinload(Project.categories))
            .where(Project.id == project.id)
        )
    ).scalar_one()
    assert sorted(c.slug for c in refreshed.categories) == ["b", "c"]


async def test_settings_categories_empty_value_clears(client, db):
    from benchlog.categories import set_project_categories
    from benchlog.models import Category

    a = Category(slug="a", name="A")
    db.add(a)
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="P",
        slug="p",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    await set_project_categories(db, project, [str(a.id)])
    await db.commit()

    await login(client, "alice")
    # The JS posts `categories=""` when the user has removed every pick;
    # that should clear the set rather than be a no-op.
    resp = await post_form(
        client,
        f"/u/alice/{project.slug}/settings",
        {"categories": ""},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 204
    db.expunge_all()
    refreshed = (
        await db.execute(
            select(Project)
            .options(selectinload(Project.categories))
            .where(Project.id == project.id)
        )
    ).scalar_one()
    assert refreshed.categories == []


async def test_settings_csrf_required(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="P",
            slug="p",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.post(
        "/u/alice/p/settings", data={"title": "No token"}
    )
    assert resp.status_code == 403


# ---------- header rendering: owner vs non-owner ---------- #


async def test_owner_sees_inline_chip_dropdowns(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Project",
            slug="p",
            status=ProjectStatus.in_progress,
            is_public=False,
            pinned=False,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/p")
    assert resp.status_code == 200
    # All three dropdown menus are rendered for the owner.
    assert "data-status-menu" in resp.text
    assert "data-public-menu" in resp.text
    assert "data-pinned-menu" in resp.text
    # Menu options for each: status has 4 values, public has make/make-private.
    assert 'data-status-option="idea"' in resp.text
    assert 'data-status-option="completed"' in resp.text
    assert 'data-public-option="1"' in resp.text
    assert 'data-public-option="0"' in resp.text
    assert 'data-pinned-option="1"' in resp.text
    assert 'data-pinned-option="0"' in resp.text


async def test_non_owner_sees_plain_chips_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    db.add(
        Project(
            user_id=alice.id,
            title="Project",
            slug="p",
            status=ProjectStatus.in_progress,
            is_public=True,
            pinned=True,
        )
    )
    await db.commit()

    await login(client, "bob")
    resp = await client.get("/u/alice/p")
    assert resp.status_code == 200
    # No owner-only dropdown markers.
    assert "data-status-menu" not in resp.text
    assert "data-public-menu" not in resp.text
    assert "data-pinned-menu" not in resp.text
    assert "data-status-option" not in resp.text
    # Plain chips still render their labels.
    assert "in progress" in resp.text
    assert "Public" in resp.text
    assert "Pinned" in resp.text


async def test_owner_sees_inline_title_edit_affordance(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Editable Title",
            slug="p",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/p")
    # Read + edit-input pairs are both present; JS swaps visibility.
    assert "data-project-title-read" in resp.text
    assert "data-project-title-edit" in resp.text
    assert "project-title-editable" in resp.text
    assert 'data-original-title="Editable Title"' in resp.text
    # Inline-edit script is loaded on the page.
    assert "project-inline-edit.js" in resp.text


async def test_non_owner_no_title_edit_markup(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    db.add(
        Project(
            user_id=alice.id,
            title="Title",
            slug="p",
            status=ProjectStatus.idea,
            is_public=True,
        )
    )
    await db.commit()

    await login(client, "bob")
    resp = await client.get("/u/alice/p")
    assert "data-project-title-edit" not in resp.text
    assert "project-title-editable" not in resp.text
    assert "project-inline-edit.js" not in resp.text


async def test_owner_more_actions_includes_slug_and_delete(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Project",
            slug="p",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/p")
    assert resp.status_code == 200
    assert "data-slug-modal-open" in resp.text
    assert "Change URL slug" in resp.text
    assert "/u/alice/p/delete" in resp.text
    assert "Delete project" in resp.text


async def test_non_owner_more_actions_has_export_only(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")
    db.add(
        Project(
            user_id=alice.id,
            title="Project",
            slug="p",
            status=ProjectStatus.idea,
            is_public=True,
        )
    )
    await db.commit()

    await login(client, "bob")
    resp = await client.get("/u/alice/p")
    assert "data-slug-modal-open" not in resp.text
    assert "Change URL slug" not in resp.text
    # Non-owner still sees export in the More actions menu.
    assert "/u/alice/p/export" in resp.text


async def test_owner_sees_manage_tag_and_category_buttons(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Project",
            slug="p",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/p")
    assert "data-project-tags-open" in resp.text
    assert "data-project-categories-open" in resp.text
    assert "Manage tags" in resp.text
    assert "Manage categories" in resp.text


async def test_non_owner_does_not_see_manage_buttons(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")

    await login(client, "alice")
    await post_form(
        client,
        "/projects",
        {"title": "P", "status": "idea", "tags": "foo", "is_public": "1"},
        csrf_path="/projects/new",
    )
    project = (
        await db.execute(select(Project).where(Project.user_id == alice.id))
    ).scalar_one()

    await login(client, "bob")
    resp = await client.get(f"/u/alice/{project.slug}")
    assert "data-project-tags-open" not in resp.text
    assert "data-project-categories-open" not in resp.text


async def test_owner_sees_related_projects_modal(client, db):
    # Smoke check: the related-projects × remove still works and the
    # confirm prompt is wired into the outgoing handler (see the inline
    # JS in projects/detail.html).
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="P",
            slug="p",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/p")
    # The relation-delete JS uses window.confirm in front of the fetch —
    # assert the guard is present so we don't silently regress to
    # one-click deletes.
    assert "window.confirm" in resp.text


async def test_settings_endpoint_json_accept_header_works(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="P",
            slug="p",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")
    token = await csrf_token(client, "/projects/new")
    resp = await client.post(
        "/u/alice/p/settings",
        data={"title": "New", "_csrf": token},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 204
