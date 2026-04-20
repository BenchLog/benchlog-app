"""OIDC routing/error paths — happy-path requires a real IdP and is out of scope."""

from sqlalchemy import select

from benchlog.auth.oidc import CallbackResult
from benchlog.models import OIDCIdentity, OIDCProvider, User
from tests.conftest import csrf_token, post_form


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


# ---------- new-user completion flow ----------


async def _make_provider(db, *, auto_create=True) -> OIDCProvider:
    provider = OIDCProvider(
        slug="g",
        display_name="Gproto",
        discovery_url="https://example.com/.well-known/openid-configuration",
        client_id="c",
        client_secret="s",
        scopes="openid profile email",
        enabled=True,
        auto_create_users=auto_create,
        auto_link_verified_email=False,
        allow_private_network=False,
    )
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    return provider


async def _walk_to_completion(
    client, monkeypatch, provider: OIDCProvider, *, profile: dict
):
    """Drive a client through /login and /callback so the session ends up with
    oidc_pending_signup populated. Patches protocol layers so no network
    traffic or crypto happens — the test owns the CallbackResult it returns.
    """
    from benchlog.auth import oidc as oidc_svc
    from benchlog.routes import oidc as oidc_route

    async def _fake_discovery(url, *, allow_private=False):
        return {
            "issuer": "https://example.com",
            "authorization_endpoint": "https://example.com/authorize",
            "jwks_uri": "https://example.com/jwks",
            "token_endpoint": "https://example.com/token",
        }

    monkeypatch.setattr(oidc_svc, "fetch_discovery", _fake_discovery)
    monkeypatch.setattr(oidc_route.oidc_svc, "fetch_discovery", _fake_discovery)
    monkeypatch.setattr(oidc_svc, "generate_state", lambda: "STATE123")
    monkeypatch.setattr(oidc_svc, "generate_nonce", lambda: "NONCE123")
    monkeypatch.setattr(
        oidc_svc, "build_authorize_url", lambda **kw: "https://example.com/authorize?x=1"
    )

    async def _fake_handle_callback(db, **kw):
        return CallbackResult(kind="needs_profile", profile_data=profile)

    monkeypatch.setattr(oidc_route.oidc_svc, "handle_callback", _fake_handle_callback)

    # Step 1 — /login stamps oidc_flow into the session cookie.
    r = await client.get(f"/auth/oidc/{provider.slug}/login")
    assert r.status_code == 302
    # Step 2 — /callback consumes oidc_flow, calls the patched handle_callback,
    # and stashes oidc_pending_signup before redirecting to /complete.
    r = await client.get(
        f"/auth/oidc/{provider.slug}/callback?code=CODE&state=STATE123"
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/oidc/complete"


async def test_complete_page_without_pending_state_redirects(client):
    r = await client.get("/auth/oidc/complete")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


async def test_complete_page_renders_prefilled_form(client, db, monkeypatch):
    provider = await _make_provider(db)
    await _walk_to_completion(
        client,
        monkeypatch,
        provider,
        profile={
            "provider_id": str(provider.id),
            "subject": "sub-abc",
            "email": "alice@test.com",
            "email_verified": True,
            "display_name_prefill": "Alice Example",
            "username_prefill": "alice",
        },
    )
    r = await client.get("/auth/oidc/complete")
    assert r.status_code == 200
    assert 'value="alice"' in r.text
    assert 'value="Alice Example"' in r.text
    # Email is shown read-only, not as a normal input.
    assert "alice@test.com" in r.text


async def test_complete_submit_creates_user_and_identity(client, db, monkeypatch):
    provider = await _make_provider(db)
    await _walk_to_completion(
        client,
        monkeypatch,
        provider,
        profile={
            "provider_id": str(provider.id),
            "subject": "sub-abc",
            "email": "alice@test.com",
            "email_verified": True,
            "display_name_prefill": "Alice Example",
            "username_prefill": "alice-prefill",
        },
    )
    token = await csrf_token(client, "/auth/oidc/complete")
    r = await client.post(
        "/auth/oidc/complete",
        data={
            "username": "cooler-name",
            "display_name": "Alice",
            "_csrf": token,
        },
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/"

    user = (await db.execute(select(User).where(User.email == "alice@test.com"))).scalar_one()
    assert user.username == "cooler-name"
    assert user.display_name == "Alice"
    assert user.email_verified is True
    assert user.password_hash is None
    # First user on a fresh DB becomes admin — matches password signup.
    assert user.is_site_admin is True

    identity = (
        await db.execute(select(OIDCIdentity).where(OIDCIdentity.user_id == user.id))
    ).scalar_one()
    assert identity.subject == "sub-abc"


async def test_complete_submit_rejects_reserved_username(client, db, monkeypatch):
    provider = await _make_provider(db)
    await _walk_to_completion(
        client,
        monkeypatch,
        provider,
        profile={
            "provider_id": str(provider.id),
            "subject": "sub-abc",
            "email": "alice@test.com",
            "email_verified": True,
            "display_name_prefill": "Alice",
            "username_prefill": "alice",
        },
    )
    r = await post_form(
        client,
        "/auth/oidc/complete",
        {"username": "admin", "display_name": "Alice"},
        csrf_path="/auth/oidc/complete",
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/oidc/complete"
    assert (await db.execute(select(User))).scalars().all() == []


async def test_complete_submit_rejects_taken_username(client, db, monkeypatch):
    # Seed an existing user whose username we'll try to take.
    from benchlog.auth.passwords import hash_password
    db.add(User(
        email="bob@test.com", username="alice", display_name="Bob",
        password_hash=hash_password("testpass1234"),
    ))
    await db.commit()

    provider = await _make_provider(db)
    await _walk_to_completion(
        client,
        monkeypatch,
        provider,
        profile={
            "provider_id": str(provider.id),
            "subject": "sub-abc",
            "email": "new@test.com",
            "email_verified": True,
            "display_name_prefill": "Alice",
            "username_prefill": "alice",
        },
    )
    r = await post_form(
        client,
        "/auth/oidc/complete",
        {"username": "alice", "display_name": "Alice"},
        csrf_path="/auth/oidc/complete",
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/oidc/complete"
    # Only the seeded user exists — no second user created.
    users = (await db.execute(select(User))).scalars().all()
    assert len(users) == 1


async def test_complete_submit_with_expired_pending_state_redirects(
    client, db, monkeypatch
):
    provider = await _make_provider(db)
    await _walk_to_completion(
        client,
        monkeypatch,
        provider,
        profile={
            "provider_id": str(provider.id),
            "subject": "sub-abc",
            "email": "alice@test.com",
            "email_verified": True,
            "display_name_prefill": "Alice",
            "username_prefill": "alice",
        },
    )
    # Fast-forward past the 10-minute TTL. The route imports time.time as
    # `_now`, so patching that attribute doesn't leak into httpx / cookiejar.
    from benchlog.routes import oidc as oidc_route
    import time as _time
    monkeypatch.setattr(oidc_route, "_now", lambda: _time.time() + 3600)
    r = await post_form(
        client,
        "/auth/oidc/complete",
        {"username": "alice", "display_name": "Alice"},
        csrf_path="/login",
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    assert (await db.execute(select(User))).scalars().all() == []
