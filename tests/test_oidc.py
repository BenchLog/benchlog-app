"""OIDC routing/error paths — happy-path requires a real IdP and is out of scope."""

from benchlog.models import OIDCProvider


async def test_login_for_unknown_provider_redirects(client):
    resp = await client.get("/auth/oidc/does-not-exist/login")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


async def test_login_for_disabled_provider_redirects(client, db):
    db.add(
        OIDCProvider(
            slug="disabled",
            display_name="Disabled",
            discovery_url="https://example.com",
            client_id="c",
            client_secret="s",
            enabled=False,
        )
    )
    await db.commit()
    resp = await client.get("/auth/oidc/disabled/login")
    assert resp.headers["location"] == "/login"


async def test_callback_with_no_session_state_redirects(client, db):
    db.add(
        OIDCProvider(
            slug="g",
            display_name="G",
            discovery_url="https://example.com",
            client_id="c",
            client_secret="s",
            enabled=True,
        )
    )
    await db.commit()
    resp = await client.get("/auth/oidc/g/callback?code=x&state=y")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


async def test_callback_passes_provider_error_through(client):
    resp = await client.get("/auth/oidc/g/callback?error=access_denied")
    assert resp.headers["location"] == "/login"
