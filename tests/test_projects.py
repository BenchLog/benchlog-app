"""Tests for project CRUD — access control, slug collisions, visibility.

Canonical URLs use /u/{username}/{slug}; creation + list live under /projects.
"""

from sqlalchemy import select

from benchlog.models import Project, ProjectStatus
from tests.conftest import csrf_token, login, make_user, post_form


async def test_home_redirects_to_projects(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    resp = await client.get("/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/projects"


async def test_project_list_requires_login(client):
    resp = await client.get("/projects")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


async def test_create_project_sets_slug_and_owner(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    resp = await post_form(
        client,
        "/projects",
        {
            "title": "Desk Lamp Restoration",
            "description": "Stripping and re-wiring a 1950s gooseneck.",
            "status": "in_progress",
            "pinned": "1",
        },
        csrf_path="/projects/new",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/desk-lamp-restoration"

    result = await db.execute(
        select(Project).where(Project.slug == "desk-lamp-restoration")
    )
    project = result.scalar_one()
    assert project.user_id == user.id
    assert project.title == "Desk Lamp Restoration"
    assert project.status == ProjectStatus.in_progress
    assert project.pinned is True


async def test_create_project_without_title_shows_error(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    resp = await post_form(
        client,
        "/projects",
        {"title": "   ", "description": "", "status": "idea"},
        csrf_path="/projects/new",
    )
    assert resp.status_code == 400
    assert "Title is required." in resp.text

    count = (await db.execute(select(Project))).scalars().all()
    assert count == []


async def test_slug_collision_within_user_appends_counter(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    for _ in range(3):
        await post_form(
            client,
            "/projects",
            {"title": "Lathe Tune-up", "description": "", "status": "idea"},
            csrf_path="/projects/new",
        )

    slugs = sorted(
        s for s in (await db.execute(select(Project.slug))).scalars().all()
    )
    assert slugs == ["lathe-tune-up", "lathe-tune-up-2", "lathe-tune-up-3"]


async def test_two_users_can_share_the_same_slug(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")

    await login(client, "alice")
    await post_form(
        client,
        "/projects",
        {"title": "Desk Lamp", "description": "", "status": "idea"},
        csrf_path="/projects/new",
    )

    # Switch to Bob and create a project with the same title.
    await login(client, "bob")
    resp = await post_form(
        client,
        "/projects",
        {"title": "Desk Lamp", "description": "", "status": "idea"},
        csrf_path="/projects/new",
    )
    assert resp.headers["location"] == "/u/bob/desk-lamp"

    slugs_by_user = {
        row.user_id: row.slug
        for row in (await db.execute(select(Project))).scalars().all()
    }
    assert slugs_by_user[alice.id] == "desk-lamp"
    assert slugs_by_user[bob.id] == "desk-lamp"


async def test_user_cannot_see_or_edit_other_users_private_project(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")

    project = Project(
        user_id=alice.id,
        title="Alice private",
        slug="alice-private",
        status=ProjectStatus.idea,
        is_public=False,
    )
    db.add(project)
    await db.commit()

    await login(client, "bob")

    # Detail — 404 (private, not owned)
    resp = await client.get("/u/alice/alice-private")
    assert resp.status_code == 404

    # Edit form — under bob's URL hits 404 (no such project under bob)
    resp = await client.get("/u/bob/alice-private/edit")
    assert resp.status_code == 404

    # Edit attempt under alice's URL — rejected since bob isn't alice
    resp = await client.get("/u/alice/alice-private/edit")
    assert resp.status_code == 404

    # Update under alice's URL — rejected
    resp = await post_form(
        client,
        "/u/alice/alice-private",
        {"title": "Hijacked", "description": "", "status": "idea"},
        csrf_path="/projects",
    )
    assert resp.status_code == 404

    # Delete under alice's URL — rejected
    resp = await post_form(
        client,
        "/u/alice/alice-private/delete",
        {},
        csrf_path="/projects",
    )
    assert resp.status_code == 404

    await db.refresh(project)
    assert project.title == "Alice private"


async def test_project_list_hides_archived_by_default(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    db.add_all(
        [
            Project(
                user_id=user.id,
                title="Live Project",
                slug="live-project",
                status=ProjectStatus.in_progress,
            ),
            Project(
                user_id=user.id,
                title="Old Shelf",
                slug="old-shelf",
                status=ProjectStatus.archived,
            ),
        ]
    )
    await db.commit()

    resp = await client.get("/projects")
    assert "Live Project" in resp.text
    assert "Old Shelf" not in resp.text

    resp = await client.get("/projects?status=archived")
    assert "Old Shelf" in resp.text
    assert "Live Project" not in resp.text


async def test_edit_project_updates_fields(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Original",
        slug="original",
        description="v1",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()

    await login(client, "alice")

    resp = await post_form(
        client,
        "/u/alice/original",
        {
            "title": "Updated Title",
            "slug": "original",
            "description": "v2 notes",
            "status": "completed",
            "pinned": "1",
        },
        csrf_path="/u/alice/original/edit",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/original"

    await db.refresh(project)
    assert project.title == "Updated Title"
    assert project.description == "v2 notes"
    assert project.status == ProjectStatus.completed
    assert project.pinned is True
    # Slug stays stable across title edits so existing URLs don't break.
    assert project.slug == "original"


async def test_delete_project_removes_row(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Disposable",
        slug="disposable",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()

    await login(client, "alice")

    resp = await post_form(
        client,
        "/u/alice/disposable/delete",
        {},
        csrf_path="/projects",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/projects"

    remaining = (await db.execute(select(Project))).scalars().all()
    assert remaining == []


async def test_new_project_defaults_to_private(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    await post_form(
        client,
        "/projects",
        {"title": "Quiet draft", "description": "", "status": "idea"},
        csrf_path="/projects/new",
    )

    result = await db.execute(select(Project).where(Project.user_id == user.id))
    project = result.scalar_one()
    assert project.is_public is False


async def test_private_project_hidden_from_other_users(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")

    db.add(
        Project(
            user_id=alice.id,
            title="Alice private build",
            slug="alice-private-build",
            status=ProjectStatus.in_progress,
            is_public=False,
        )
    )
    await db.commit()

    await login(client, "bob")

    resp = await client.get("/u/alice/alice-private-build")
    assert resp.status_code == 404

    resp = await client.get("/explore")
    assert "Alice private build" not in resp.text


async def test_public_project_visible_to_others(client, db):
    alice = await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    await make_user(db, email="bob@test.com", username="bob")

    db.add(
        Project(
            user_id=alice.id,
            title="Workshop stool by Alice",
            slug="alice-workshop-stool",
            description="Shop-made from scrap oak.",
            status=ProjectStatus.in_progress,
            is_public=True,
        )
    )
    await db.commit()

    await login(client, "bob")

    resp = await client.get("/u/alice/alice-workshop-stool")
    assert resp.status_code == 200
    assert "Workshop stool by Alice" in resp.text
    # Non-owners see the byline and do not see the Edit button
    assert "by Alice" in resp.text
    assert "/u/alice/alice-workshop-stool/edit" not in resp.text

    resp = await client.get("/explore")
    assert "Workshop stool by Alice" in resp.text
    assert "by Alice" in resp.text


async def test_username_match_is_case_insensitive(client, db):
    alice = await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    db.add(
        Project(
            user_id=alice.id,
            title="Case demo",
            slug="case-demo",
            status=ProjectStatus.in_progress,
            is_public=True,
        )
    )
    await db.commit()

    resp = await client.get("/u/ALICE/case-demo")
    assert resp.status_code == 200
    assert "Case demo" in resp.text


async def test_non_owner_cannot_edit_public_project(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")

    project = Project(
        user_id=alice.id,
        title="Alice public shelf",
        slug="alice-public-shelf",
        status=ProjectStatus.in_progress,
        is_public=True,
    )
    db.add(project)
    await db.commit()

    await login(client, "bob")

    # Edit URL under alice is rejected because bob isn't alice
    resp = await client.get("/u/alice/alice-public-shelf/edit")
    assert resp.status_code == 404

    # Update attempt is rejected
    resp = await post_form(
        client,
        "/u/alice/alice-public-shelf",
        {"title": "Pwned", "description": "", "status": "idea", "is_public": "1"},
        csrf_path="/projects",
    )
    assert resp.status_code == 404

    await db.refresh(project)
    assert project.title == "Alice public shelf"


async def test_explore_excludes_archived_public_projects(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")

    db.add_all(
        [
            Project(
                user_id=alice.id,
                title="Live and public",
                slug="live-and-public",
                status=ProjectStatus.in_progress,
                is_public=True,
            ),
            Project(
                user_id=alice.id,
                title="Old archived public",
                slug="old-archived-public",
                status=ProjectStatus.archived,
                is_public=True,
            ),
        ]
    )
    await db.commit()

    await login(client, "bob")
    resp = await client.get("/explore")
    assert "Live and public" in resp.text
    assert "Old archived public" not in resp.text


async def test_explore_is_open_to_guests(client, db):
    alice = await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    db.add_all(
        [
            Project(
                user_id=alice.id,
                title="Public build",
                slug="public-build",
                status=ProjectStatus.in_progress,
                is_public=True,
            ),
            Project(
                user_id=alice.id,
                title="Secret build",
                slug="secret-build",
                status=ProjectStatus.in_progress,
                is_public=False,
            ),
        ]
    )
    await db.commit()

    resp = await client.get("/explore")
    assert resp.status_code == 200
    assert "Public build" in resp.text
    assert "Secret build" not in resp.text
    # Guest nav exposes the sign-in CTA, not My Projects
    assert 'href="/login"' in resp.text
    assert "My Projects" not in resp.text


async def test_guest_can_view_public_project_detail(client, db):
    alice = await make_user(
        db, email="alice@test.com", username="alice", display_name="Alice"
    )
    db.add(
        Project(
            user_id=alice.id,
            title="Open bench grinder",
            slug="open-bench-grinder",
            description="Resurrected from a flea-market find.",
            status=ProjectStatus.in_progress,
            is_public=True,
        )
    )
    await db.commit()

    resp = await client.get("/u/alice/open-bench-grinder")
    assert resp.status_code == 200
    assert "Open bench grinder" in resp.text
    assert "by Alice" in resp.text
    assert "/u/alice/open-bench-grinder/edit" not in resp.text


async def test_guest_cannot_view_private_project(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=alice.id,
            title="Private notebook",
            slug="private-notebook",
            status=ProjectStatus.idea,
            is_public=False,
        )
    )
    await db.commit()

    resp = await client.get("/u/alice/private-notebook")
    assert resp.status_code == 404


async def test_guest_edit_endpoints_still_require_login(client, db):
    alice = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=alice.id,
            title="Anything",
            slug="anything",
            status=ProjectStatus.idea,
            is_public=True,
        )
    )
    await db.commit()

    # /projects (list) — still auth-gated
    resp = await client.get("/projects")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"

    # /projects/new — still auth-gated
    resp = await client.get("/projects/new")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"

    # Edit form — still auth-gated
    resp = await client.get("/u/alice/anything/edit")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"

    # Update (POST) — still auth-gated
    resp = await client.post(
        "/u/alice/anything", data={"title": "Hijacked", "status": "idea"}
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


async def test_owner_can_toggle_public_via_edit(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Toggleable",
        slug="toggleable",
        status=ProjectStatus.idea,
        is_public=False,
    )
    db.add(project)
    await db.commit()

    await login(client, "alice")

    await post_form(
        client,
        "/u/alice/toggleable",
        {
            "title": "Toggleable",
            "slug": "toggleable",
            "description": "",
            "status": "idea",
            "is_public": "1",
        },
        csrf_path="/u/alice/toggleable/edit",
    )
    await db.refresh(project)
    assert project.is_public is True

    # Unchecking the checkbox omits the field entirely → bool(None) is False
    await post_form(
        client,
        "/u/alice/toggleable",
        {
            "title": "Toggleable",
            "slug": "toggleable",
            "description": "",
            "status": "idea",
        },
        csrf_path="/u/alice/toggleable/edit",
    )
    await db.refresh(project)
    assert project.is_public is False


async def test_create_with_custom_slug_uses_it(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    resp = await post_form(
        client,
        "/projects",
        {
            "title": "Desk Lamp Restoration",
            "slug": "brass-lamp",
            "description": "",
            "status": "idea",
        },
        csrf_path="/projects/new",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/brass-lamp"

    project = (await db.execute(select(Project))).scalar_one()
    assert project.slug == "brass-lamp"


async def test_create_with_custom_slug_normalizes_input(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    resp = await post_form(
        client,
        "/projects",
        {
            "title": "Anything",
            "slug": "  My Weird SLUG!  ",
            "description": "",
            "status": "idea",
        },
        csrf_path="/projects/new",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/my-weird-slug"

    project = (await db.execute(select(Project))).scalar_one()
    assert project.slug == "my-weird-slug"


async def test_create_with_unusable_slug_shows_error(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    resp = await post_form(
        client,
        "/projects",
        {
            "title": "Anything",
            "slug": "!!!",
            "description": "",
            "status": "idea",
        },
        csrf_path="/projects/new",
    )
    assert resp.status_code == 400
    assert "Slug must contain letters or numbers." in resp.text
    # Title and slug values are preserved in the form so the user can fix it
    assert 'value="Anything"' in resp.text
    assert 'value="!!!"' in resp.text

    count = (await db.execute(select(Project))).scalars().all()
    assert count == []


async def test_create_slug_collision_same_user_shows_error(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Existing",
            slug="workbench",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")

    resp = await post_form(
        client,
        "/projects",
        {
            "title": "Another Workbench",
            "slug": "workbench",
            "description": "",
            "status": "idea",
        },
        csrf_path="/projects/new",
    )
    assert resp.status_code == 400
    assert "already used by another of your projects" in resp.text

    # Only the pre-existing project remains
    slugs = sorted(
        s for s in (await db.execute(select(Project.slug))).scalars().all()
    )
    assert slugs == ["workbench"]


async def test_edit_can_rename_slug_and_redirects_to_new_url(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Brass Lamp",
        slug="desk-lamp",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()

    await login(client, "alice")

    resp = await post_form(
        client,
        "/u/alice/desk-lamp",
        {
            "title": "Brass Lamp",
            "slug": "brass-lamp",
            "description": "",
            "status": "idea",
        },
        csrf_path="/u/alice/desk-lamp/edit",
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/u/alice/brass-lamp"

    await db.refresh(project)
    assert project.slug == "brass-lamp"

    # Old URL 404s
    resp = await client.get("/u/alice/desk-lamp")
    assert resp.status_code == 404

    # New URL works
    resp = await client.get("/u/alice/brass-lamp")
    assert resp.status_code == 200


async def test_edit_blank_slug_shows_error(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Has slug",
            slug="has-slug",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")

    resp = await post_form(
        client,
        "/u/alice/has-slug",
        {"title": "Has slug", "slug": "", "description": "", "status": "idea"},
        csrf_path="/u/alice/has-slug/edit",
    )
    assert resp.status_code == 400
    assert "Slug is required." in resp.text


async def test_edit_slug_collision_within_user_shows_error(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add_all(
        [
            Project(
                user_id=user.id,
                title="First",
                slug="first",
                status=ProjectStatus.idea,
            ),
            Project(
                user_id=user.id,
                title="Second",
                slug="second",
                status=ProjectStatus.idea,
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")

    resp = await post_form(
        client,
        "/u/alice/second",
        {"title": "Second", "slug": "first", "description": "", "status": "idea"},
        csrf_path="/u/alice/second/edit",
    )
    assert resp.status_code == 400
    assert "already used by another of your projects" in resp.text

    # The conflicting slug is preserved in the form so the user can fix it
    assert 'value="first"' in resp.text


async def test_edit_form_includes_slug_change_warning(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Has slug",
            slug="has-slug",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/u/alice/has-slug/edit")
    assert resp.status_code == 200
    # Hidden-by-default warning markup is present; JS toggles it on slug edit.
    assert "data-slug-warning" in resp.text
    assert 'data-original-slug="has-slug"' in resp.text
    assert "will stop working" in resp.text


async def test_new_project_form_has_no_slug_change_warning(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    resp = await client.get("/projects/new")
    assert resp.status_code == 200
    # New form has no original slug to warn against, so no warning block.
    assert "data-slug-warning" not in resp.text
    assert "data-original-slug" not in resp.text


async def test_edit_keeping_existing_slug_saves_without_collision(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    project = Project(
        user_id=user.id,
        title="Original",
        slug="keep-me",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()

    await login(client, "alice")

    # Submitting the same slug back should save cleanly (no self-collision).
    resp = await post_form(
        client,
        "/u/alice/keep-me",
        {
            "title": "Updated Title",
            "slug": "keep-me",
            "description": "",
            "status": "idea",
        },
        csrf_path="/u/alice/keep-me/edit",
    )
    assert resp.status_code == 302

    await db.refresh(project)
    assert project.slug == "keep-me"
    assert project.title == "Updated Title"


async def test_deleting_user_cascades_to_projects(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Attached",
            slug="attached",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")
    token = await csrf_token(client, "/account")
    resp = await client.post(
        "/account/delete",
        data={
            "confirm_username": "alice",
            "current_password": "testpass1234",
            "_csrf": token,
        },
    )
    assert resp.status_code == 302

    remaining = (await db.execute(select(Project))).scalars().all()
    assert remaining == []
