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
        csrf_path="/projects/new",
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
        csrf_path="/projects/new",
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
        csrf_path="/projects/new",
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
        csrf_path="/projects/new",
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
        csrf_path="/projects/new",
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
        csrf_path="/projects/new",
    )
    assert resp.status_code == 400
    assert "already used by another of your projects" in resp.text

    # The conflicting slug is preserved in the form so the user can fix it
    assert 'value="first"' in resp.text


async def test_detail_includes_slug_change_modal_warning_for_owner(client, db):
    # Slug-change warning now lives in a modal surfaced from the More
    # actions menu — only rendered for the owner on the detail page.
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
    resp = await client.get("/u/alice/has-slug")
    assert resp.status_code == 200
    # Modal markup is present; JS toggles the warning text when the slug
    # input diverges from the original.
    assert "data-slug-modal" in resp.text
    assert 'data-original-slug="has-slug"' in resp.text
    assert "will stop working" in resp.text


async def test_edit_page_route_is_gone(client, db):
    # The standalone edit page has been replaced by inline controls — any
    # GET to /edit should now 404.
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
    assert resp.status_code == 404


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
        csrf_path="/projects/new",
    )
    assert resp.status_code == 302

    await db.refresh(project)
    assert project.slug == "keep-me"
    assert project.title == "Updated Title"


# ---------- filter sidebar: multi-select + visibility + chips ---------- #


async def test_list_filter_multiple_statuses(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add_all(
        [
            Project(
                user_id=user.id,
                title="Idea A",
                slug="idea-a",
                status=ProjectStatus.idea,
            ),
            Project(
                user_id=user.id,
                title="Doing B",
                slug="doing-b",
                status=ProjectStatus.in_progress,
            ),
            Project(
                user_id=user.id,
                title="Done C",
                slug="done-c",
                status=ProjectStatus.completed,
            ),
            Project(
                user_id=user.id,
                title="Gone D",
                slug="gone-d",
                status=ProjectStatus.archived,
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")

    resp = await client.get("/projects?status=idea&status=completed")
    assert "Idea A" in resp.text
    assert "Done C" in resp.text
    assert "Doing B" not in resp.text
    # Archived excluded by default even without status filter — also excluded
    # here because archived isn't in the explicit list.
    assert "Gone D" not in resp.text

    # No status filter: archived hidden, others visible
    resp = await client.get("/projects")
    assert "Idea A" in resp.text
    assert "Doing B" in resp.text
    assert "Done C" in resp.text
    assert "Gone D" not in resp.text


async def test_list_filter_single_tag(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    await post_form(
        client,
        "/projects",
        {"title": "Has foo", "description": "", "status": "idea", "tags": "foo"},
        csrf_path="/projects/new",
    )
    await post_form(
        client,
        "/projects",
        {"title": "No tags", "description": "", "status": "idea", "tags": ""},
        csrf_path="/projects/new",
    )

    resp = await client.get("/projects?tag=foo")
    assert "Has foo" in resp.text
    assert "No tags" not in resp.text


async def test_list_filter_multiple_tags_requires_all(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    for title, tags in [
        ("Has A and B", "a, b"),
        ("Has A and C", "a, c"),
        ("Has A only", "a"),
    ]:
        await post_form(
            client,
            "/projects",
            {"title": title, "description": "", "status": "idea", "tags": tags},
            csrf_path="/projects/new",
        )

    resp = await client.get("/projects?tag=a&tag=b")
    assert "Has A and B" in resp.text
    assert "Has A and C" not in resp.text
    assert "Has A only" not in resp.text


async def test_list_filter_tag_mode_any_is_union(client, db):
    # With tag_mode=any, a project needs only ONE of the selected tags.
    # Useful for corralling spelling variants ("3d-printing" OR "3d-printed").
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    for title, tags in [
        ("Has A and B", "a, b"),
        ("Has A and C", "a, c"),
        ("Has only D", "d"),
    ]:
        await post_form(
            client,
            "/projects",
            {"title": title, "description": "", "status": "idea", "tags": tags},
            csrf_path="/projects/new",
        )

    resp = await client.get("/projects?tag=b&tag=c&tag_mode=any")
    assert "Has A and B" in resp.text
    assert "Has A and C" in resp.text
    assert "Has only D" not in resp.text


async def test_list_filter_tag_mode_defaults_to_all(client, db):
    # Omitting tag_mode (or passing unknown value) keeps the legacy AND
    # behaviour so existing bookmarks/links don't silently broaden.
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")
    for title, tags in [("Has A and B", "a, b"), ("Has only A", "a")]:
        await post_form(
            client,
            "/projects",
            {"title": title, "description": "", "status": "idea", "tags": tags},
            csrf_path="/projects/new",
        )

    # No tag_mode — AND
    resp = await client.get("/projects?tag=a&tag=b")
    assert "Has A and B" in resp.text
    assert "Has only A" not in resp.text
    # tag_mode=bogus — also AND
    resp = await client.get("/projects?tag=a&tag=b&tag_mode=bogus")
    assert "Has A and B" in resp.text
    assert "Has only A" not in resp.text


async def test_list_filter_visibility_public(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add_all(
        [
            Project(
                user_id=user.id,
                title="Pub One",
                slug="pub-one",
                status=ProjectStatus.idea,
                is_public=True,
            ),
            Project(
                user_id=user.id,
                title="Pub Two",
                slug="pub-two",
                status=ProjectStatus.in_progress,
                is_public=True,
            ),
            Project(
                user_id=user.id,
                title="Priv One",
                slug="priv-one",
                status=ProjectStatus.idea,
                is_public=False,
            ),
            Project(
                user_id=user.id,
                title="Priv Two",
                slug="priv-two",
                status=ProjectStatus.in_progress,
                is_public=False,
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/projects?visibility=public")
    assert "Pub One" in resp.text
    assert "Pub Two" in resp.text
    assert "Priv One" not in resp.text
    assert "Priv Two" not in resp.text


async def test_list_filter_visibility_private(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add_all(
        [
            Project(
                user_id=user.id,
                title="Pub One",
                slug="pub-one",
                status=ProjectStatus.idea,
                is_public=True,
            ),
            Project(
                user_id=user.id,
                title="Pub Two",
                slug="pub-two",
                status=ProjectStatus.in_progress,
                is_public=True,
            ),
            Project(
                user_id=user.id,
                title="Priv One",
                slug="priv-one",
                status=ProjectStatus.idea,
                is_public=False,
            ),
            Project(
                user_id=user.id,
                title="Priv Two",
                slug="priv-two",
                status=ProjectStatus.in_progress,
                is_public=False,
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/projects?visibility=private")
    assert "Priv One" in resp.text
    assert "Priv Two" in resp.text
    assert "Pub One" not in resp.text
    assert "Pub Two" not in resp.text


async def test_list_known_tags_are_user_scoped(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await make_user(db, email="bob@test.com", username="bob")

    # Alice seeds tags a, b
    await login(client, "alice")
    await post_form(
        client,
        "/projects",
        {"title": "Alice P", "description": "", "status": "idea", "tags": "a, b"},
        csrf_path="/projects/new",
    )

    # Bob seeds tag c
    await login(client, "bob")
    await post_form(
        client,
        "/projects",
        {"title": "Bob P", "description": "", "status": "idea", "tags": "c"},
        csrf_path="/projects/new",
    )

    # Back to alice — her combobox should list a,b but not c
    await login(client, "alice")
    resp = await client.get("/projects")
    assert resp.status_code == 200
    import re

    known_match = re.search(r'data-known-tags="([^"]*)"', resp.text)
    assert known_match is not None
    slugs = set(known_match.group(1).split())
    assert slugs == {"a", "b"}
    assert "c" not in slugs


async def test_list_filter_active_state_rendered_in_bar(client, db):
    # In the top-bar layout, active filters show up as pressed pills /
    # selected radios / tag chips inside the popover — users toggle them off
    # by clicking the pill again, not by clicking a separate chip row.
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    await post_form(
        client,
        "/projects",
        {
            "title": "Tagged idea",
            "description": "",
            "status": "idea",
            "tags": "foo",
        },
        csrf_path="/projects/new",
    )

    import re
    resp = await client.get("/projects?status=idea&tag=foo")
    body = resp.text
    # Status pill for "idea" is rendered checked (attrs may split across lines).
    assert re.search(r'value="idea"\s+checked', body)
    # Tag "foo" appears as a selected pill inside the popover.
    assert 'value="foo"' in body
    # A Clear-all link is rendered when any filter is active.
    assert 'href="/projects"' in body and "Clear all" in body


async def test_list_filter_combining_status_tag_visibility(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    # Use the form endpoint so tags attach cleanly.
    await post_form(
        client,
        "/projects",
        {
            "title": "Match me",
            "description": "",
            "status": "in_progress",
            "tags": "foo",
            "is_public": "1",
        },
        csrf_path="/projects/new",
    )
    await post_form(
        client,
        "/projects",
        {
            "title": "Wrong status",
            "description": "",
            "status": "idea",
            "tags": "foo",
            "is_public": "1",
        },
        csrf_path="/projects/new",
    )
    await post_form(
        client,
        "/projects",
        {
            "title": "Wrong tag",
            "description": "",
            "status": "in_progress",
            "tags": "bar",
            "is_public": "1",
        },
        csrf_path="/projects/new",
    )
    await post_form(
        client,
        "/projects",
        {
            "title": "Wrong visibility",
            "description": "",
            "status": "in_progress",
            "tags": "foo",
        },
        csrf_path="/projects/new",
    )
    # Sanity — 4 projects attached to alice
    from sqlalchemy import func as _func

    count = (
        await db.execute(
            select(_func.count(Project.id)).where(Project.user_id == user.id)
        )
    ).scalar_one()
    assert count == 4

    resp = await client.get(
        "/projects?status=in_progress&tag=foo&visibility=public"
    )
    assert "Match me" in resp.text
    assert "Wrong status" not in resp.text
    assert "Wrong tag" not in resp.text
    assert "Wrong visibility" not in resp.text


async def test_list_filter_ignores_unknown_status(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Visible",
            slug="visible",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/projects?status=bogus")
    # Unknown value ignored → behaves as no filter → default (exclude archived)
    assert resp.status_code == 200
    assert "Visible" in resp.text


# ---------- full-text search: /projects?q= ---------- #


async def test_list_search_matches_title(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add_all(
        [
            Project(
                user_id=user.id,
                title="Inlay fixture",
                slug="inlay-fixture",
                status=ProjectStatus.idea,
            ),
            Project(
                user_id=user.id,
                title="Router jig",
                slug="router-jig",
                status=ProjectStatus.idea,
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/projects?q=inlay")
    assert resp.status_code == 200
    assert "Inlay fixture" in resp.text
    assert "Router jig" not in resp.text


async def test_list_search_matches_description(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add_all(
        [
            Project(
                user_id=user.id,
                title="Weekend plans",
                slug="weekend-plans",
                description="Cutting dovetails with a router",
                status=ProjectStatus.idea,
            ),
            Project(
                user_id=user.id,
                title="Desk build",
                slug="desk-build",
                description="Gluing up panels",
                status=ProjectStatus.idea,
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/projects?q=router")
    assert "Weekend plans" in resp.text
    assert "Desk build" not in resp.text


async def test_list_search_multi_word_is_and(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add_all(
        [
            Project(
                user_id=user.id,
                title="Inlay fixture for router",
                slug="inlay-fixture-router",
                status=ProjectStatus.idea,
            ),
            Project(
                user_id=user.id,
                title="Router jig",
                slug="router-jig-2",
                status=ProjectStatus.idea,
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/projects?q=inlay+router")
    assert "Inlay fixture for router" in resp.text
    assert "Router jig" not in resp.text


async def test_list_search_is_substring_match(client, db):
    # Substring matching: a partial token anywhere in the title hits —
    # "flowi" finds "Flowire" (prefix), and "wire" also finds "Flowire"
    # (infix). Matches the mental model of a maker-journal search.
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add_all(
        [
            Project(
                user_id=user.id,
                title="Flowire",
                slug="flowire",
                status=ProjectStatus.idea,
            ),
            Project(
                user_id=user.id,
                title="Dovetail jig",
                slug="dovetail-jig",
                status=ProjectStatus.idea,
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/projects?q=flowi")
    assert "Flowire" in resp.text
    assert "Dovetail jig" not in resp.text

    # Infix match: "wire" is inside "Flowire".
    resp = await client.get("/projects?q=wire")
    assert "Flowire" in resp.text
    assert "Dovetail jig" not in resp.text

    # Single-letter tokens are dropped (too broad), so behaves like no-search.
    resp = await client.get("/projects?q=f")
    assert "Flowire" in resp.text
    assert "Dovetail jig" in resp.text


async def test_list_search_combines_with_status_filter(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add_all(
        [
            Project(
                user_id=user.id,
                title="Router jig completed",
                slug="router-done",
                status=ProjectStatus.completed,
            ),
            Project(
                user_id=user.id,
                title="Router plan idea",
                slug="router-idea",
                status=ProjectStatus.idea,
            ),
            Project(
                user_id=user.id,
                title="Lathe overhaul completed",
                slug="lathe-done",
                status=ProjectStatus.completed,
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/projects?q=router&status=completed")
    assert "Router jig completed" in resp.text
    assert "Router plan idea" not in resp.text
    assert "Lathe overhaul completed" not in resp.text


async def test_list_search_combines_with_tag_filter(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    for title, tags in [
        ("Router jig with fixture", "fixture"),
        ("Router table", "bench"),
        ("Lathe with fixture", "fixture"),
    ]:
        await post_form(
            client,
            "/projects",
            {"title": title, "description": "", "status": "idea", "tags": tags},
            csrf_path="/projects/new",
        )

    resp = await client.get("/projects?q=router&tag=fixture")
    assert "Router jig with fixture" in resp.text
    assert "Router table" not in resp.text  # wrong tag
    assert "Lathe with fixture" not in resp.text  # wrong search


async def test_list_search_empty_string_behaves_like_no_search(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add_all(
        [
            Project(
                user_id=user.id,
                title="Apple pie",
                slug="apple-pie",
                status=ProjectStatus.idea,
            ),
            Project(
                user_id=user.id,
                title="Banana bread",
                slug="banana-bread",
                status=ProjectStatus.idea,
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")
    # Whitespace-only q is treated as absent — all non-archived show up.
    resp = await client.get("/projects?q=%20%20%20")
    assert "Apple pie" in resp.text
    assert "Banana bread" in resp.text


async def test_list_search_returns_empty_for_no_matches(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Inlay fixture",
            slug="inlay-fixture",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/projects?q=zzznotfound")
    assert resp.status_code == 200
    assert "Inlay fixture" not in resp.text
    assert "No projects match these filters." in resp.text


async def test_list_search_ordering_by_relevance_overrides_pinned(client, db):
    # Pinned project without the term should NOT appear first when q is set —
    # relevance wins. Without q, the pinned one is first.
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add_all(
        [
            Project(
                user_id=user.id,
                title="Pinned unrelated",
                slug="pinned-unrelated",
                description="something completely different",
                status=ProjectStatus.idea,
                pinned=True,
            ),
            Project(
                user_id=user.id,
                title="Non-pinned with keyword dovetail",
                slug="dovetail-match",
                status=ProjectStatus.idea,
                pinned=False,
            ),
        ]
    )
    await db.commit()

    await login(client, "alice")

    # With q=dovetail, only the non-pinned match shows up (and therefore is
    # also first). Proves relevance filter + ordering.
    resp = await client.get("/projects?q=dovetail")
    body = resp.text
    assert "Non-pinned with keyword dovetail" in body
    assert "Pinned unrelated" not in body

    # No q: both render, and pinned comes first in page order.
    resp = await client.get("/projects")
    body = resp.text
    idx_pinned = body.find("Pinned unrelated")
    idx_other = body.find("Non-pinned with keyword dovetail")
    assert idx_pinned != -1 and idx_other != -1
    assert idx_pinned < idx_other


async def test_list_search_input_rendered_in_filter_bar(client, db):
    # Smoke check: the filter bar renders a <input name="q"> and echoes any
    # active query back into its value attribute.
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    resp = await client.get("/projects")
    assert 'name="q"' in resp.text
    assert 'placeholder="Search projects' in resp.text

    resp = await client.get("/projects?q=widgets")
    assert 'value="widgets"' in resp.text
    # ✕ clear link shows when q is active, and it clears back to base_url.
    assert 'class="filter-search-clear"' in resp.text
    # Clear-all link also renders (since q counts as an active filter).
    assert "Clear all" in resp.text


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


# ---------- categories ----------


async def _seed_cat(db, **kwargs):
    from benchlog.models import Category

    cat = Category(**kwargs)
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    return cat


async def test_create_project_with_categories(client, db):
    import uuid as _uuid

    from sqlalchemy.orm import selectinload

    parent = await _seed_cat(db, slug="3d-printing", name="3D Printing")
    fdm = await _seed_cat(
        db, slug="fdm", name="FDM", parent_id=parent.id
    )
    assert isinstance(fdm.id, _uuid.UUID)

    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    await post_form(
        client,
        "/projects",
        {
            "title": "FDM Printer Upgrade",
            "description": "",
            "status": "idea",
            "category": [str(fdm.id)],
        },
        csrf_path="/projects/new",
    )

    project = (
        await db.execute(
            select(Project)
            .options(selectinload(Project.categories))
            .where(Project.slug == "fdm-printer-upgrade")
        )
    ).scalar_one()
    assert [c.slug for c in project.categories] == ["fdm"]


async def test_edit_project_categories_replace_semantics(client, db):
    from sqlalchemy.orm import selectinload

    from benchlog.categories import set_project_categories

    a = await _seed_cat(db, slug="a", name="A")
    b = await _seed_cat(db, slug="b", name="B")
    c = await _seed_cat(db, slug="c", name="C")

    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    project = Project(
        user_id=user.id,
        title="Iterating",
        slug="iterating",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    await set_project_categories(db, project, [str(a.id), str(b.id)])
    await db.commit()

    await post_form(
        client,
        f"/u/alice/{project.slug}",
        {
            "title": "Iterating",
            "slug": project.slug,
            "description": "",
            "status": "idea",
            "category": [str(b.id), str(c.id)],
        },
        csrf_path="/projects/new",
    )
    # Identity map holds pre-request state with expire_on_commit=False.
    db.expunge_all()
    refreshed = (
        await db.execute(
            select(Project)
            .options(selectinload(Project.categories))
            .where(Project.id == project.id)
        )
    ).scalar_one()
    assert sorted(cat.slug for cat in refreshed.categories) == ["b", "c"]


async def test_project_form_renders_known_category_options(client, db):
    parent = await _seed_cat(db, slug="woodworking", name="Woodworking")
    await _seed_cat(db, slug="joinery", name="Joinery", parent_id=parent.id)

    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    resp = await client.get("/projects/new")
    assert 'data-category-input' in resp.text
    # Combobox emits the breadcrumb as a JSON array of segments (not the
    # joined `›` string) so the client JS can render Lucide chevrons
    # between parts. Assert both segments appear in the options payload.
    assert '"Woodworking"' in resp.text
    assert '"Joinery"' in resp.text


async def test_create_project_drops_unknown_category_ids_silently(client, db):
    import uuid as _uuid

    from sqlalchemy.orm import selectinload

    real = await _seed_cat(db, slug="real", name="Real")

    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    bogus_uuid = str(_uuid.uuid4())
    await post_form(
        client,
        "/projects",
        {
            "title": "Silent Drop",
            "description": "",
            "status": "idea",
            "category": [str(real.id), bogus_uuid, "not-a-uuid"],
        },
        csrf_path="/projects/new",
    )

    project = (
        await db.execute(
            select(Project)
            .options(selectinload(Project.categories))
            .where(Project.slug == "silent-drop")
        )
    ).scalar_one()
    assert [c.slug for c in project.categories] == ["real"]


async def test_list_filter_by_single_category(client, db):
    from benchlog.categories import set_project_categories

    wood = await _seed_cat(db, slug="woodworking", name="Woodworking")
    elec = await _seed_cat(db, slug="electronics", name="Electronics")

    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    p_wood = Project(
        user_id=user.id,
        title="Wood Project",
        slug="wood-project",
        status=ProjectStatus.in_progress,
    )
    p_elec = Project(
        user_id=user.id,
        title="Elec Project",
        slug="elec-project",
        status=ProjectStatus.in_progress,
    )
    db.add_all([p_wood, p_elec])
    await db.commit()
    await db.refresh(p_wood)
    await db.refresh(p_elec)
    await set_project_categories(db, p_wood, [str(wood.id)])
    await set_project_categories(db, p_elec, [str(elec.id)])
    await db.commit()

    resp = await client.get(f"/projects?category={wood.id}")
    assert "Wood Project" in resp.text
    assert "Elec Project" not in resp.text


async def test_list_filter_by_multiple_categories_requires_all(client, db):
    from benchlog.categories import set_project_categories

    a = await _seed_cat(db, slug="a", name="A")
    b = await _seed_cat(db, slug="b", name="B")

    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    both = Project(
        user_id=user.id, title="Both", slug="both", status=ProjectStatus.idea
    )
    only_a = Project(
        user_id=user.id, title="Only A", slug="only-a", status=ProjectStatus.idea
    )
    db.add_all([both, only_a])
    await db.commit()
    await db.refresh(both)
    await db.refresh(only_a)
    await set_project_categories(db, both, [str(a.id), str(b.id)])
    await set_project_categories(db, only_a, [str(a.id)])
    await db.commit()

    resp = await client.get(f"/projects?category={a.id}&category={b.id}")
    assert "Both" in resp.text
    assert "Only A" not in resp.text


async def test_list_filter_category_mode_any_is_union(client, db):
    # With category_mode=any, a project matches when it carries ANY of the
    # selected categories. Useful for "show me anything under the 3D
    # Printing subtree" — pick FDM + Resin, flip to any.
    from benchlog.categories import set_project_categories

    a = await _seed_cat(db, slug="a", name="A")
    b = await _seed_cat(db, slug="b", name="B")
    c = await _seed_cat(db, slug="c", name="C")

    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    in_a = Project(
        user_id=user.id, title="In A", slug="in-a", status=ProjectStatus.idea
    )
    in_b = Project(
        user_id=user.id, title="In B", slug="in-b", status=ProjectStatus.idea
    )
    in_c = Project(
        user_id=user.id, title="In C", slug="in-c", status=ProjectStatus.idea
    )
    db.add_all([in_a, in_b, in_c])
    await db.commit()
    for p in (in_a, in_b, in_c):
        await db.refresh(p)
    await set_project_categories(db, in_a, [str(a.id)])
    await set_project_categories(db, in_b, [str(b.id)])
    await set_project_categories(db, in_c, [str(c.id)])
    await db.commit()

    # any-mode: request A OR B → C is excluded, both A-only and B-only show.
    resp = await client.get(
        f"/projects?category={a.id}&category={b.id}&category_mode=any"
    )
    assert "In A" in resp.text
    assert "In B" in resp.text
    assert "In C" not in resp.text


async def test_list_filter_category_mode_defaults_to_all(client, db):
    # Omitting category_mode (or passing unknown value) keeps legacy AND
    # so existing bookmarks / old URLs don't silently broaden.
    from benchlog.categories import set_project_categories

    a = await _seed_cat(db, slug="a", name="A")
    b = await _seed_cat(db, slug="b", name="B")

    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")
    both = Project(
        user_id=user.id, title="Both", slug="both", status=ProjectStatus.idea
    )
    only_a = Project(
        user_id=user.id, title="Only A", slug="only-a", status=ProjectStatus.idea
    )
    db.add_all([both, only_a])
    await db.commit()
    await db.refresh(both)
    await db.refresh(only_a)
    await set_project_categories(db, both, [str(a.id), str(b.id)])
    await set_project_categories(db, only_a, [str(a.id)])
    await db.commit()

    # No category_mode → AND
    resp = await client.get(f"/projects?category={a.id}&category={b.id}")
    assert "Both" in resp.text
    assert "Only A" not in resp.text
    # Bogus category_mode → also AND
    resp = await client.get(
        f"/projects?category={a.id}&category={b.id}&category_mode=bogus"
    )
    assert "Both" in resp.text
    assert "Only A" not in resp.text


async def test_explore_filter_by_category_only_shows_public(client, db):
    from benchlog.categories import set_project_categories

    a = await _seed_cat(db, slug="a", name="A")

    user = await make_user(db, email="alice@test.com", username="alice")

    pub = Project(
        user_id=user.id,
        title="Pub",
        slug="pub",
        status=ProjectStatus.in_progress,
        is_public=True,
    )
    priv = Project(
        user_id=user.id,
        title="Priv",
        slug="priv",
        status=ProjectStatus.in_progress,
        is_public=False,
    )
    db.add_all([pub, priv])
    await db.commit()
    await db.refresh(pub)
    await db.refresh(priv)
    await set_project_categories(db, pub, [str(a.id)])
    await set_project_categories(db, priv, [str(a.id)])
    await db.commit()

    resp = await client.get(f"/explore?category={a.id}")
    assert "Pub" in resp.text
    assert "Priv" not in resp.text


async def test_filter_composes_with_status_and_tag(client, db):
    from benchlog.categories import set_project_categories
    from benchlog.tags import set_project_tags

    a = await _seed_cat(db, slug="a", name="A")

    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    match = Project(
        user_id=user.id,
        title="Match",
        slug="match",
        status=ProjectStatus.completed,
    )
    wrong_status = Project(
        user_id=user.id,
        title="WrongStatus",
        slug="wrong-status",
        status=ProjectStatus.idea,
    )
    no_tag = Project(
        user_id=user.id,
        title="NoTag",
        slug="no-tag",
        status=ProjectStatus.completed,
    )
    db.add_all([match, wrong_status, no_tag])
    await db.commit()
    for p in (match, wrong_status, no_tag):
        await db.refresh(p, ["tags"])  # raise_on_sql — preload before assign
        await set_project_categories(db, p, [str(a.id)])
    await set_project_tags(db, match, ["woodworking"])
    await set_project_tags(db, wrong_status, ["woodworking"])
    await db.commit()

    resp = await client.get(
        f"/projects?category={a.id}&status=completed&tag=woodworking"
    )
    assert "Match" in resp.text
    assert "WrongStatus" not in resp.text
    assert "NoTag" not in resp.text


async def test_project_card_shows_leaf_name_with_breadcrumb_tooltip(client, db):
    from benchlog.categories import set_project_categories

    parent = await _seed_cat(db, slug="3d-printing", name="3D Printing")
    fdm = await _seed_cat(
        db, slug="fdm", name="FDM", parent_id=parent.id
    )

    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    project = Project(
        user_id=user.id,
        title="Printer",
        slug="printer",
        status=ProjectStatus.idea,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    await set_project_categories(db, project, [str(fdm.id)])
    await db.commit()

    resp = await client.get("/projects")
    # Leaf name "FDM" appears on the chip with breadcrumb in title=.
    assert "FDM" in resp.text
    assert 'title="3D Printing \u203a FDM"' in resp.text


async def test_project_header_shows_full_breadcrumb_chips(client, db):
    from benchlog.categories import set_project_categories

    parent = await _seed_cat(db, slug="3d-printing", name="3D Printing")
    fdm = await _seed_cat(
        db, slug="fdm", name="FDM", parent_id=parent.id
    )

    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    project = Project(
        user_id=user.id,
        title="Printer",
        slug="printer",
        status=ProjectStatus.idea,
        is_public=True,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    await set_project_categories(db, project, [str(fdm.id)])
    await db.commit()

    resp = await client.get("/u/alice/printer")
    # Full breadcrumb in the chip body for the detail header.
    assert "3D Printing \u203a FDM" in resp.text


async def test_project_header_breadcrumbs_on_every_tab(client, db):
    """Every tab that renders ``projects/_layout.html`` must pass the same
    ``category_breadcrumbs`` into the header — otherwise a category like
    ``3D Printing › FDM`` collapses to just ``FDM`` on the journal/
    files tabs while showing the full trail on overview. This regression
    fires any time a new tab forgets the shared header context dict.
    """
    from benchlog.categories import set_project_categories

    parent = await _seed_cat(db, slug="3d-printing", name="3D Printing")
    fdm = await _seed_cat(db, slug="fdm", name="FDM", parent_id=parent.id)

    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    project = Project(
        user_id=user.id,
        title="Printer",
        slug="printer",
        status=ProjectStatus.idea,
        is_public=True,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    await set_project_categories(db, project, [str(fdm.id)])
    await db.commit()

    # Hit each non-overview tab — the shared header must render the full
    # breadcrumb (title= attr on the chip + both segments in the body).
    expected_title = 'title="3D Printing › FDM"'
    for path in (
        "/u/alice/printer/journal",
        "/u/alice/printer/files",
        "/u/alice/printer/gallery",
        "/u/alice/printer/links",
        "/u/alice/printer/activity",
    ):
        resp = await client.get(path)
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"
        assert expected_title in resp.text, (
            f"{path} missing breadcrumb tooltip — category_breadcrumbs not threaded"
        )
        assert ">3D Printing<" in resp.text, f"{path} missing parent segment"
        assert ">FDM<" in resp.text, f"{path} missing leaf segment"


async def test_card_hides_category_row_when_empty(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(
        Project(
            user_id=user.id,
            title="Bare",
            slug="bare",
            status=ProjectStatus.idea,
        )
    )
    await db.commit()

    await login(client, "alice")
    resp = await client.get("/projects")
    assert 'aria-label="Categories"' not in resp.text
