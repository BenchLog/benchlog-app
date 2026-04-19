"""Tests for /account — password change and OIDC unlinking guards."""

from sqlalchemy import select

from benchlog.models import OIDCIdentity, OIDCProvider, User
from benchlog.auth.passwords import verify_password
from tests.conftest import csrf_token, login, make_user


async def test_profile_update_cannot_change_username(client, db):
    """Username is immutable: extra `username` field in POST must be ignored."""
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    token = await csrf_token(client, "/account")
    resp = await client.post(
        "/account/profile",
        data={
            "email": "alice@test.com",
            "username": "mallory",  # attacker-supplied, should be ignored
            "display_name": "Alice",
            "_csrf": token,
        },
    )
    assert resp.headers["location"] == "/account"

    await db.refresh(user)
    assert user.username == "alice"
    assert user.display_name == "Alice"


async def test_change_password_requires_current(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    token = await csrf_token(client, "/account")
    resp = await client.post(
        "/account/password",
        data={
            "current_password": "wrong-current",
            "password": "brand-new-pw",
            "password_confirm": "brand-new-pw",
            "_csrf": token,
        },
    )
    assert resp.headers["location"] == "/account"

    await db.refresh(user)
    assert verify_password("testpass1234", user.password_hash) is True
    assert verify_password("brand-new-pw", user.password_hash) is False


async def test_change_password_succeeds(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    token = await csrf_token(client, "/account")
    resp = await client.post(
        "/account/password",
        data={
            "current_password": "testpass1234",
            "password": "brand-new-pw",
            "password_confirm": "brand-new-pw",
            "_csrf": token,
        },
    )
    assert resp.headers["location"] == "/account"

    await db.refresh(user)
    assert verify_password("brand-new-pw", user.password_hash) is True


async def test_change_password_mismatch_rejected(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    token = await csrf_token(client, "/account")
    await client.post(
        "/account/password",
        data={
            "current_password": "testpass1234",
            "password": "brand-new-pw",
            "password_confirm": "different-pw",
            "_csrf": token,
        },
    )
    await db.refresh(user)
    assert verify_password("testpass1234", user.password_hash) is True


async def test_change_password_too_short_rejected(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    token = await csrf_token(client, "/account")
    await client.post(
        "/account/password",
        data={
            "current_password": "testpass1234",
            "password": "short",
            "password_confirm": "short",
            "_csrf": token,
        },
    )
    await db.refresh(user)
    assert verify_password("testpass1234", user.password_hash) is True


async def test_delete_password_blocked_when_only_method(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    token = await csrf_token(client, "/account")
    resp = await client.post("/account/password/delete", data={"_csrf": token})
    assert resp.headers["location"] == "/account"

    await db.refresh(user)
    assert user.password_hash is not None


async def test_delete_password_succeeds_with_oidc_linked(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    provider = OIDCProvider(
        slug="test-idp",
        display_name="Test IdP",
        discovery_url="https://example.com/.well-known/openid-configuration",
        client_id="cid",
        client_secret="secret",
        enabled=True,
    )
    db.add(provider)
    await db.flush()
    db.add(
        OIDCIdentity(
            user_id=user.id, provider_id=provider.id, subject="abc-123", email=user.email
        )
    )
    await db.commit()

    await login(client, "alice")
    token = await csrf_token(client, "/account")
    resp = await client.post("/account/password/delete", data={"_csrf": token})
    assert resp.headers["location"] == "/account"

    await db.refresh(user)
    assert user.password_hash is None


async def test_delete_password_noop_when_already_unset(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")
    user.password_hash = None
    await db.commit()

    token = await csrf_token(client, "/account")
    resp = await client.post("/account/password/delete", data={"_csrf": token})
    assert resp.headers["location"] == "/account"

    await db.refresh(user)
    assert user.password_hash is None


async def test_unlink_only_method_blocked_when_no_password(client, db):
    """An OIDC-only user can't unlink their last identity — would lock them out."""
    user = await make_user(db, email="alice@test.com", username="alice")
    provider = OIDCProvider(
        slug="test-idp",
        display_name="Test IdP",
        discovery_url="https://example.com/.well-known/openid-configuration",
        client_id="cid",
        client_secret="secret",
        enabled=True,
    )
    db.add(provider)
    await db.flush()
    identity = OIDCIdentity(
        user_id=user.id, provider_id=provider.id, subject="abc-123", email=user.email
    )
    db.add(identity)
    await db.commit()

    # Log in with the password, then null it out to simulate an OIDC-only user.
    await login(client, "alice")
    user.password_hash = None
    await db.commit()

    token = await csrf_token(client, "/account")
    resp = await client.post(
        f"/account/oidc/{identity.id}/unlink", data={"_csrf": token}
    )
    assert resp.headers["location"] == "/account"

    remaining = (
        await db.execute(select(OIDCIdentity).where(OIDCIdentity.id == identity.id))
    ).scalar_one_or_none()
    assert remaining is not None


async def test_unlink_succeeds_when_password_present(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    provider = OIDCProvider(
        slug="test-idp",
        display_name="Test IdP",
        discovery_url="https://example.com/.well-known/openid-configuration",
        client_id="cid",
        client_secret="secret",
        enabled=True,
    )
    db.add(provider)
    await db.flush()
    identity = OIDCIdentity(
        user_id=user.id, provider_id=provider.id, subject="abc-123", email=user.email
    )
    db.add(identity)
    await db.commit()

    await login(client, "alice")
    token = await csrf_token(client, "/account")
    resp = await client.post(
        f"/account/oidc/{identity.id}/unlink", data={"_csrf": token}
    )
    assert resp.headers["location"] == "/account"

    remaining = (
        await db.execute(select(OIDCIdentity).where(OIDCIdentity.id == identity.id))
    ).scalar_one_or_none()
    assert remaining is None


async def test_cannot_unlink_other_users_identity(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    bob = await make_user(db, email="bob@test.com", username="bob")
    provider = OIDCProvider(
        slug="test-idp",
        display_name="Test IdP",
        discovery_url="https://example.com/.well-known/openid-configuration",
        client_id="cid",
        client_secret="secret",
        enabled=True,
    )
    db.add(provider)
    await db.flush()
    bob_identity = OIDCIdentity(
        user_id=bob.id, provider_id=provider.id, subject="bob-sub", email=bob.email
    )
    db.add(bob_identity)
    await db.commit()

    await login(client, "alice")
    token = await csrf_token(client, "/account")
    resp = await client.post(
        f"/account/oidc/{bob_identity.id}/unlink", data={"_csrf": token}
    )
    assert resp.headers["location"] == "/account"

    # Bob's identity still present
    still = (
        await db.execute(
            select(OIDCIdentity).where(OIDCIdentity.id == bob_identity.id)
        )
    ).scalar_one_or_none()
    assert still is not None


# ---------- self-delete ----------


async def test_self_delete_requires_username_confirmation(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    token = await csrf_token(client, "/account")
    resp = await client.post(
        "/account/delete",
        data={
            "confirm_username": "wrong",
            "current_password": "testpass1234",
            "_csrf": token,
        },
    )
    assert resp.headers["location"] == "/account"

    still = (
        await db.execute(select(User).where(User.id == user.id))
    ).scalar_one_or_none()
    assert still is not None


async def test_self_delete_requires_correct_password(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")

    token = await csrf_token(client, "/account")
    resp = await client.post(
        "/account/delete",
        data={
            "confirm_username": "alice",
            "current_password": "wrong-password",
            "_csrf": token,
        },
    )
    assert resp.headers["location"] == "/account"

    still = (
        await db.execute(select(User).where(User.id == user.id))
    ).scalar_one_or_none()
    assert still is not None


async def test_self_delete_succeeds(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    user_id = user.id
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
    assert resp.headers["location"] == "/login"

    gone = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    assert gone is None

    # Session was cleared — subsequent / hits the login redirect
    home = await client.get("/")
    assert home.headers["location"] == "/login"


async def test_self_delete_blocked_for_last_admin(client, db):
    sole_admin = await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    await login(client, "admin")

    token = await csrf_token(client, "/account")
    resp = await client.post(
        "/account/delete",
        data={
            "confirm_username": "admin",
            "current_password": "testpass1234",
            "_csrf": token,
        },
    )
    assert resp.headers["location"] == "/account"

    still = (
        await db.execute(select(User).where(User.id == sole_admin.id))
    ).scalar_one_or_none()
    assert still is not None


async def test_self_delete_allowed_for_admin_when_other_admin_exists(client, db):
    admin1 = await make_user(
        db, email="admin1@test.com", username="admin1", is_site_admin=True
    )
    await make_user(
        db, email="admin2@test.com", username="admin2", is_site_admin=True
    )
    await login(client, "admin1")

    token = await csrf_token(client, "/account")
    resp = await client.post(
        "/account/delete",
        data={
            "confirm_username": "admin1",
            "current_password": "testpass1234",
            "_csrf": token,
        },
    )
    assert resp.headers["location"] == "/login"

    gone = (
        await db.execute(select(User).where(User.id == admin1.id))
    ).scalar_one_or_none()
    assert gone is None


async def test_self_delete_passwordless_user_skips_password_check(client, db):
    """OIDC/passkey-only users have no password — password field is unused."""
    user = await make_user(db, email="alice@test.com", username="alice")
    await login(client, "alice")
    # After authenticating, strip the password to simulate a passwordless user.
    user.password_hash = None
    await db.commit()

    token = await csrf_token(client, "/account")
    resp = await client.post(
        "/account/delete",
        data={"confirm_username": "alice", "_csrf": token},
    )
    assert resp.headers["location"] == "/login"

    gone = (
        await db.execute(select(User).where(User.id == user.id))
    ).scalar_one_or_none()
    assert gone is None
