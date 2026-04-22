"""Tests for the shared HTML error page (404, 5xx, etc).

The exception handler renders `errors/error.html` which extends `base.html`.
Since `base.html`'s nav switches on `{% if user %}`, the handler must resolve
the current session user into the template context — otherwise logged-in
viewers hitting a bad URL see the guest navbar.
"""

from tests.conftest import login, make_user


async def test_404_shows_user_navbar_when_logged_in(client, db):
    """Logged-in users hitting a 404 should still see the user nav
    (My Projects / Collections / Explore + user menu)."""
    await make_user(db, email="alice@test.com", username="alice", display_name="Alice")
    await login(client, "alice")

    resp = await client.get(
        "/definitely-not-a-real-page",
        headers={"accept": "text/html"},
    )
    assert resp.status_code == 404
    # My Projects link is the load-bearing user-nav marker — only rendered
    # when the template has `user`.
    assert 'href="/projects"' in resp.text
    # Sign-in button is the guest-nav marker — must NOT be present when
    # the user is logged in.
    assert 'href="/login"' not in resp.text


async def test_404_shows_guest_navbar_when_signed_out(client, db):
    """Guests hitting a 404 see the guest nav with a sign-in link.

    Use a 404 under `/u/{username}` (covered by the public-view gate) so
    AuthMiddleware doesn't redirect the guest to /login before the
    exception handler gets a chance to render the error page.
    """
    resp = await client.get(
        "/u/nobody-at-all",
        headers={"accept": "text/html"},
    )
    assert resp.status_code == 404
    assert 'href="/login"' in resp.text


async def test_404_owner_scoped_private_project_renders_with_user_nav(client, db):
    """A 404 from the owner-scoped private-project gate (viewer isn't the
    owner) should still render the shared error page with the viewer's
    user nav, not the guest nav."""
    owner = await make_user(
        db, email="owner@test.com", username="owner", display_name="Owner"
    )
    await make_user(
        db, email="bob@test.com", username="bob", display_name="Bob"
    )

    from benchlog.models import Project, ProjectStatus

    db.add(
        Project(
            user_id=owner.id,
            title="Private",
            slug="private",
            status=ProjectStatus.idea,
            is_public=False,
        )
    )
    await db.commit()

    await login(client, "bob")
    resp = await client.get(
        "/u/owner/private",
        headers={"accept": "text/html"},
    )
    assert resp.status_code == 404
    assert 'href="/projects"' in resp.text
    assert 'href="/login"' not in resp.text
