"""Tests for the AuthMiddleware gate and admin authorization."""

import pytest

from tests.conftest import csrf_token, login, make_user


PUBLIC_GETS = ["/login", "/signup"]
PROTECTED_GETS = [
    "/",
    "/account",
    "/admin",
    "/admin/users",
    "/admin/oidc",
    "/admin/oidc/new",
    "/admin/smtp",
    "/admin/settings",
]


@pytest.mark.parametrize("path", PUBLIC_GETS)
async def test_public_pages_reachable_without_login(client, path):
    resp = await client.get(path)
    assert resp.status_code == 200


@pytest.mark.parametrize("path", PROTECTED_GETS)
async def test_protected_pages_redirect_when_unauthenticated(client, path):
    resp = await client.get(path)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


async def test_static_files_reachable_without_login(client):
    resp = await client.get("/static/css/output.css")
    assert resp.status_code in (200, 404)  # ok if file missing in CI; we care it's not a redirect


@pytest.mark.parametrize(
    "path",
    [
        "/admin",
        "/admin/users",
        "/admin/oidc",
        "/admin/oidc/new",
        "/admin/smtp",
        "/admin/settings",
    ],
)
async def test_non_admin_user_forbidden_from_admin(client, db, path):
    await make_user(db, email="bob@test.com", username="bob", is_site_admin=False)
    await login(client, "bob")
    resp = await client.get(path)
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/admin",
        "/admin/users",
        "/admin/oidc",
        "/admin/smtp",
        "/admin/settings",
    ],
)
async def test_admin_user_can_reach_admin_pages(client, db, path):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    await login(client, "admin")
    resp = await client.get(path)
    # /admin redirects to /admin/users; the others render
    assert resp.status_code in (200, 302)


async def test_admin_post_action_forbidden_for_non_admin(client, db):
    await make_user(db, email="bob@test.com", username="bob", is_site_admin=False)
    target = await make_user(db, email="t@test.com", username="target")
    await login(client, "bob")
    token = await csrf_token(client, "/account")
    resp = await client.post(
        f"/admin/users/{target.id}/toggle-active", data={"_csrf": token}
    )
    assert resp.status_code == 403


async def test_account_post_requires_login(client):
    resp = await client.post(
        "/account/password",
        data={"password": "newpassword12", "password_confirm": "newpassword12"},
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"
