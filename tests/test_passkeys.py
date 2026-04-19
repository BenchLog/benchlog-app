"""Tests for passkey route gates and last-sign-in-method protection.

The end-to-end WebAuthn ceremony requires a real (or simulated) authenticator,
which is out of scope. We test:
- registration endpoints require login
- login endpoints don't require login
- registration_start returns valid options JSON containing a challenge
- delete protections (can't delete only sign-in method, can't touch other users')
"""

import base64

from sqlalchemy import select

from benchlog.models import OIDCIdentity, OIDCProvider, WebAuthnCredential
from tests.conftest import csrf_token, login, make_user


def _fake_credential(user_id, *, credential_id: bytes = b"cred-id-bytes-1") -> WebAuthnCredential:
    return WebAuthnCredential(
        user_id=user_id,
        credential_id=credential_id,
        public_key=b"\x00fake-public-key",
        sign_count=0,
        transports="internal",
        friendly_name="Test passkey",
    )


# ---------- auth gates ----------


async def test_register_start_requires_login(client):
    # Auth runs before CSRF — unauthenticated POST redirects to /login.
    resp = await client.post("/account/passkeys/register/start")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


async def test_register_finish_requires_login(client):
    resp = await client.post(
        "/account/passkeys/register/finish",
        json={"id": "x"},
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


async def test_delete_requires_login(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    cred = _fake_credential(user.id)
    db.add(cred)
    await db.commit()

    resp = await client.post(f"/account/passkeys/{cred.id}/delete")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


async def test_passkey_login_endpoints_are_public(client):
    token = await csrf_token(client, "/login")
    resp = await client.post(
        "/auth/passkey/start", headers={"X-CSRF-Token": token}
    )
    assert resp.status_code == 200  # returns options JSON
    body = resp.json()
    assert "challenge" in body


# ---------- registration options ----------


async def test_register_start_returns_options_with_challenge(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    token = await csrf_token(client, "/account")
    resp = await client.post(
        "/account/passkeys/register/start",
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "challenge" in body
    assert body["rp"]["id"]  # base_url-derived
    assert body["user"]["name"] == "alice@test.com"


async def test_register_start_excludes_already_registered_credentials(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    db.add(_fake_credential(user.id, credential_id=b"existing-cred-1"))
    await db.commit()
    await login(client, "alice")

    token = await csrf_token(client, "/account")
    resp = await client.post(
        "/account/passkeys/register/start",
        headers={"X-CSRF-Token": token},
    )
    body = resp.json()
    excluded = body.get("excludeCredentials") or []
    assert len(excluded) == 1


# ---------- delete protections ----------


async def test_delete_passkey_blocked_when_only_sign_in_method(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    cred = _fake_credential(user.id)
    db.add(cred)
    await db.commit()

    await login(client, "alice")
    user.password_hash = None  # passkey is now the only method
    await db.commit()

    token = await csrf_token(client, "/account")
    resp = await client.post(
        f"/account/passkeys/{cred.id}/delete", data={"_csrf": token}
    )
    assert resp.headers["location"] == "/account"

    still = (
        await db.execute(
            select(WebAuthnCredential).where(WebAuthnCredential.id == cred.id)
        )
    ).scalar_one_or_none()
    assert still is not None


async def test_delete_passkey_allowed_when_password_present(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    cred = _fake_credential(user.id)
    db.add(cred)
    await db.commit()
    await login(client, "alice")

    token = await csrf_token(client, "/account")
    resp = await client.post(
        f"/account/passkeys/{cred.id}/delete", data={"_csrf": token}
    )
    assert resp.headers["location"] == "/account"

    gone = (
        await db.execute(
            select(WebAuthnCredential).where(WebAuthnCredential.id == cred.id)
        )
    ).scalar_one_or_none()
    assert gone is None


async def test_delete_passkey_allowed_when_other_passkey_present(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    cred1 = _fake_credential(user.id, credential_id=b"cred-1")
    cred2 = _fake_credential(user.id, credential_id=b"cred-2")
    db.add_all([cred1, cred2])
    await db.commit()

    await login(client, "alice")
    user.password_hash = None
    await db.commit()

    token = await csrf_token(client, "/account")
    resp = await client.post(
        f"/account/passkeys/{cred1.id}/delete", data={"_csrf": token}
    )
    assert resp.headers["location"] == "/account"

    remaining = (
        await db.execute(
            select(WebAuthnCredential).where(WebAuthnCredential.user_id == user.id)
        )
    ).scalars().all()
    assert len(list(remaining)) == 1


async def test_delete_passkey_allowed_when_oidc_linked(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    cred = _fake_credential(user.id)
    db.add(cred)
    provider = OIDCProvider(
        slug="g",
        display_name="G",
        discovery_url="https://example.com",
        client_id="c",
        client_secret="s",
    )
    db.add(provider)
    await db.flush()
    db.add(
        OIDCIdentity(
            user_id=user.id, provider_id=provider.id, subject="abc", email=user.email
        )
    )
    await db.commit()

    await login(client, "alice")
    user.password_hash = None
    await db.commit()

    token = await csrf_token(client, "/account")
    resp = await client.post(
        f"/account/passkeys/{cred.id}/delete", data={"_csrf": token}
    )
    assert resp.headers["location"] == "/account"

    gone = (
        await db.execute(
            select(WebAuthnCredential).where(WebAuthnCredential.id == cred.id)
        )
    ).scalar_one_or_none()
    assert gone is None


async def test_cannot_delete_other_users_passkey(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    bob_cred = _fake_credential(bob.id, credential_id=b"bob-cred")
    db.add(bob_cred)
    await db.commit()

    await login(client, "alice")
    token = await csrf_token(client, "/account")
    resp = await client.post(
        f"/account/passkeys/{bob_cred.id}/delete", data={"_csrf": token}
    )
    assert resp.headers["location"] == "/account"

    still = (
        await db.execute(
            select(WebAuthnCredential).where(WebAuthnCredential.id == bob_cred.id)
        )
    ).scalar_one_or_none()
    assert still is not None


# ---------- rename ----------


async def test_rename_passkey_updates_friendly_name(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    cred = _fake_credential(user.id)
    db.add(cred)
    await db.commit()
    await login(client, "alice")

    token = await csrf_token(client, "/account")
    resp = await client.post(
        f"/account/passkeys/{cred.id}/rename",
        data={"_csrf": token, "friendly_name": "  Work laptop  "},
    )
    assert resp.headers["location"] == "/account"

    await db.refresh(cred)
    assert cred.friendly_name == "Work laptop"


async def test_rename_passkey_blank_falls_back_to_default(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    cred = _fake_credential(user.id)
    db.add(cred)
    await db.commit()
    await login(client, "alice")

    token = await csrf_token(client, "/account")
    await client.post(
        f"/account/passkeys/{cred.id}/rename",
        data={"_csrf": token, "friendly_name": "   "},
    )
    await db.refresh(cred)
    assert cred.friendly_name == "Passkey"


async def test_cannot_rename_other_users_passkey(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    bob_cred = _fake_credential(bob.id, credential_id=b"bob-cred")
    db.add(bob_cred)
    await db.commit()

    await login(client, "alice")
    token = await csrf_token(client, "/account")
    resp = await client.post(
        f"/account/passkeys/{bob_cred.id}/rename",
        data={"_csrf": token, "friendly_name": "Pwned"},
    )
    assert resp.headers["location"] == "/account"

    await db.refresh(bob_cred)
    assert bob_cred.friendly_name == "Test passkey"


# ---------- OIDC unlink protection now considers passkeys ----------


async def test_unlink_oidc_allowed_when_passkey_present(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    provider = OIDCProvider(
        slug="g",
        display_name="G",
        discovery_url="https://example.com",
        client_id="c",
        client_secret="s",
        enabled=True,
    )
    db.add(provider)
    await db.flush()
    identity = OIDCIdentity(
        user_id=user.id, provider_id=provider.id, subject="abc", email=user.email
    )
    db.add(identity)
    db.add(_fake_credential(user.id))
    await db.commit()

    await login(client, "alice")
    user.password_hash = None
    await db.commit()

    token = await csrf_token(client, "/account")
    resp = await client.post(
        f"/account/oidc/{identity.id}/unlink", data={"_csrf": token}
    )
    assert resp.headers["location"] == "/account"

    gone = (
        await db.execute(
            select(OIDCIdentity).where(OIDCIdentity.id == identity.id)
        )
    ).scalar_one_or_none()
    assert gone is None


# ---------- passkey login finish, basic error cases ----------


async def test_auth_finish_without_session_state(client):
    token = await csrf_token(client, "/login")
    resp = await client.post(
        "/auth/passkey/finish",
        json={"rawId": base64.urlsafe_b64encode(b"x").decode().rstrip("=")},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 400


async def test_auth_finish_rejects_non_increasing_sign_count(client, db, monkeypatch):
    """Stale or cloned authenticator: new sign_count must strictly exceed stored."""
    from types import SimpleNamespace

    user = await make_user(db, email="alice@test.com", username="alice")
    cred = _fake_credential(user.id)
    cred.sign_count = 10
    db.add(cred)
    await db.commit()
    await db.refresh(cred)

    from benchlog.routes import passkeys as routes

    def fake_verify(credential, expected_challenge, stored):
        # Returned counter equals stored (not strictly greater) — should be rejected.
        return SimpleNamespace(new_sign_count=10)

    monkeypatch.setattr(routes.wa, "verify_authentication", fake_verify)

    token = await csrf_token(client, "/login")
    await client.post("/auth/passkey/start", headers={"X-CSRF-Token": token})
    raw_id = base64.urlsafe_b64encode(cred.credential_id).decode().rstrip("=")
    resp = await client.post(
        "/auth/passkey/finish",
        json={"rawId": raw_id},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 400

    await db.refresh(cred)
    assert cred.sign_count == 10  # not advanced


async def test_auth_finish_allows_zero_sign_count_authenticators(client, db, monkeypatch):
    """Some authenticators don't implement counters — both counts stay 0."""
    from types import SimpleNamespace

    user = await make_user(db, email="alice@test.com", username="alice")
    cred = _fake_credential(user.id)
    cred.sign_count = 0
    db.add(cred)
    await db.commit()
    await db.refresh(cred)

    from benchlog.routes import passkeys as routes

    def fake_verify(credential, expected_challenge, stored):
        return SimpleNamespace(new_sign_count=0)

    monkeypatch.setattr(routes.wa, "verify_authentication", fake_verify)

    token = await csrf_token(client, "/login")
    await client.post("/auth/passkey/start", headers={"X-CSRF-Token": token})
    raw_id = base64.urlsafe_b64encode(cred.credential_id).decode().rstrip("=")
    resp = await client.post(
        "/auth/passkey/finish",
        json={"rawId": raw_id},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 200


async def test_auth_finish_unknown_credential(client):
    # Prime the session with a challenge by hitting /start
    token = await csrf_token(client, "/login")
    await client.post("/auth/passkey/start", headers={"X-CSRF-Token": token})
    resp = await client.post(
        "/auth/passkey/finish",
        json={"rawId": base64.urlsafe_b64encode(b"unknown-cred").decode().rstrip("=")},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 400


# ---------- passkey signup ----------


SIGNUP_START_FORM = {
    "email": "new@test.com",
    "username": "new",
    "display_name": "New User",
}


async def test_signup_passkey_start_returns_options_for_first_user(client):
    token = await csrf_token(client, "/signup")
    resp = await client.post(
        "/signup/passkey/start",
        data=SIGNUP_START_FORM,
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "challenge" in body
    assert body["user"]["name"] == "new@test.com"


async def test_signup_passkey_start_blocked_when_signup_disabled(client, db):
    from benchlog.models import SiteSettings

    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    db.add(SiteSettings(allow_local_signup=False))
    await db.commit()

    token = await csrf_token(client, "/signup")
    resp = await client.post(
        "/signup/passkey/start",
        data=SIGNUP_START_FORM,
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 403


async def test_signup_passkey_start_duplicate_email_is_indistinguishable(client, db):
    """Email collision must not be detectable — response matches the happy path."""
    from benchlog.models import SiteSettings

    await make_user(db, email="taken@test.com", username="existing")
    db.add(SiteSettings(allow_local_signup=True))
    await db.commit()

    token = await csrf_token(client, "/signup")
    resp = await client.post(
        "/signup/passkey/start",
        data={**SIGNUP_START_FORM, "email": "taken@test.com"},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "challenge" in body


async def test_signup_passkey_start_rejects_duplicate_username(client, db):
    from benchlog.models import SiteSettings

    await make_user(db, email="other@test.com", username="new")
    db.add(SiteSettings(allow_local_signup=True))
    await db.commit()

    token = await csrf_token(client, "/signup")
    resp = await client.post(
        "/signup/passkey/start",
        data=SIGNUP_START_FORM,
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 400


async def test_signup_passkey_finish_without_pending(client):
    token = await csrf_token(client, "/signup")
    resp = await client.post(
        "/signup/passkey/finish", json={}, headers={"X-CSRF-Token": token}
    )
    assert resp.status_code == 400


async def test_signup_passkey_start_rejects_when_already_logged_in(client, db):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    await login(client, "admin")
    token = await csrf_token(client, "/account")
    resp = await client.post(
        "/signup/passkey/start",
        data=SIGNUP_START_FORM,
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 400
