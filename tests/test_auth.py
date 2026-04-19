"""Tests for local signup, login, logout, password reset, email verification."""
import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from benchlog.models import EmailToken, SiteSettings, SMTPConfig, User
from tests.conftest import csrf_token, login, make_user


def _token_hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


async def _signup(client, payload):
    token = await csrf_token(client, "/signup")
    return await client.post("/signup", data={**payload, "_csrf": token})


# ---------- signup ----------


async def test_first_signup_becomes_admin_and_logs_in(client, signup_payload, db):
    resp = await _signup(client, signup_payload)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"

    user = (await db.execute(select(User))).scalar_one()
    assert user.is_site_admin is True
    assert user.email_verified is True  # first user bypasses verification
    assert user.password_hash is not None

    # Cookie persisted: hitting / redirects to /projects (logged in), not /login
    home = await client.get("/")
    assert home.headers["location"] == "/projects"


async def test_second_signup_blocked_when_signup_disabled(client, db, signup_payload):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    db.add(SiteSettings(allow_local_signup=False))
    await db.commit()

    resp = await _signup(client, signup_payload)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"

    # Confirm no second user was created
    count = (await db.execute(select(User))).scalars().all()
    assert len(count) == 1


async def test_second_signup_allowed_when_enabled(client, db, signup_payload):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    db.add(SiteSettings(allow_local_signup=True))
    await db.commit()

    resp = await _signup(client, signup_payload)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"

    users = (await db.execute(select(User))).scalars().all()
    assert len(users) == 2
    new = next(u for u in users if u.email == "first@test.com")
    assert new.is_site_admin is False  # only the very first becomes admin


async def test_signup_password_mismatch(client, signup_payload, db):
    signup_payload["password_confirm"] = "different"
    resp = await _signup(client, signup_payload)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/signup"
    assert (await db.execute(select(User))).first() is None


async def test_signup_password_too_short(client, signup_payload, db):
    signup_payload["password"] = "short"
    signup_payload["password_confirm"] = "short"
    resp = await _signup(client, signup_payload)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/signup"
    assert (await db.execute(select(User))).first() is None


async def test_signup_invalid_email(client, signup_payload, db):
    signup_payload["email"] = "not-an-email"
    resp = await _signup(client, signup_payload)
    assert resp.headers["location"] == "/signup"
    assert (await db.execute(select(User))).first() is None


async def test_signup_duplicate_email(client, db, signup_payload):
    """Email collision must not be observable to the attacker: the response
    looks like a successful signup, and no new user is created."""
    await make_user(db, email="first@test.com", username="other")
    db.add(SiteSettings(allow_local_signup=True))
    await db.commit()

    resp = await _signup(client, signup_payload)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"
    users = (await db.execute(select(User))).scalars().all()
    assert len(users) == 1


async def test_signup_duplicate_username(client, db, signup_payload):
    await make_user(db, email="other@test.com", username="first")
    db.add(SiteSettings(allow_local_signup=True))
    await db.commit()

    resp = await _signup(client, signup_payload)
    assert resp.headers["location"] == "/signup"
    users = (await db.execute(select(User))).scalars().all()
    assert len(users) == 1


async def test_validate_username_rejects_invalid_slugs():
    """Slug rules: no spaces, symbols, or leading/trailing separators; length 2-32."""
    import pytest

    from benchlog.auth.signup import SignupValidationError, validate_username

    for bad in ["has space", "bob!", "-bob", "bob-", "_bob", "bob_", "a", "x" * 33, ""]:
        with pytest.raises(SignupValidationError):
            validate_username(bad)


async def test_validate_username_normalizes_valid_slugs():
    """Input is lowercased and trimmed; internal hyphens/underscores are fine."""
    from benchlog.auth.signup import validate_username

    assert validate_username("  Alice_Smith-01  ") == "alice_smith-01"
    assert validate_username("AB") == "ab"
    assert validate_username("a1") == "a1"


# ---------- login ----------


async def test_login_with_email(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    resp = await login(client, "alice@test.com")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


async def test_login_with_username(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    resp = await login(client, "alice")
    assert resp.headers["location"] == "/"


async def test_login_wrong_password_redirects_to_login(client, db):
    await make_user(db, email="alice@test.com", username="alice")
    resp = await login(client, "alice", "wrong-password")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"

    # And confirm we're not actually logged in
    home = await client.get("/")
    assert home.headers["location"] == "/login"


async def test_login_unknown_user(client):
    resp = await login(client, "ghost@test.com", "whatever1234")
    assert resp.headers["location"] == "/login"


async def test_login_disabled_account(client, db):
    await make_user(db, email="alice@test.com", username="alice", is_active=False)
    resp = await login(client, "alice")
    assert resp.headers["location"] == "/login"


async def test_login_blocked_when_email_verification_required(client, db):
    await make_user(
        db, email="alice@test.com", username="alice", email_verified=False
    )
    db.add(SiteSettings(require_email_verification=True))
    await db.commit()

    resp = await login(client, "alice")
    assert resp.headers["location"] == "/login"


async def test_login_allowed_when_verification_required_but_user_verified(client, db):
    await make_user(
        db, email="alice@test.com", username="alice", email_verified=True
    )
    db.add(SiteSettings(require_email_verification=True))
    await db.commit()

    resp = await login(client, "alice")
    assert resp.headers["location"] == "/"


async def test_login_with_oidc_only_user_rejected(client, db):
    """Users without password_hash (OIDC-only) can't use local login."""
    await make_user(db, email="alice@test.com", username="alice", password=None)
    resp = await login(client, "alice", "anything12345")
    assert resp.headers["location"] == "/login"


# ---------- logout ----------


async def test_logout_clears_session(client, db, signup_payload):
    await _signup(client, signup_payload)
    assert (await client.get("/")).headers["location"] == "/projects"

    token = await csrf_token(client, "/projects")
    resp = await client.post("/logout", data={"_csrf": token})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"

    home = await client.get("/")
    assert home.headers["location"] == "/login"


# ---------- email verification ----------


async def test_verify_token_marks_user_verified(client, db):
    user = await make_user(
        db, email="alice@test.com", username="alice", email_verified=False
    )
    token = EmailToken(
        user_id=user.id,
        token_hash=_token_hash("verify-me-12345"),
        purpose="verify",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(token)
    await db.commit()

    resp = await client.get("/auth/verify/verify-me-12345")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"

    await db.refresh(user)
    assert user.email_verified is True


async def test_verify_token_expired_does_not_verify(client, db):
    user = await make_user(
        db, email="alice@test.com", username="alice", email_verified=False
    )
    token = EmailToken(
        user_id=user.id,
        token_hash=_token_hash("expired-1234"),
        purpose="verify",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.add(token)
    await db.commit()

    await client.get("/auth/verify/expired-1234")
    await db.refresh(user)
    assert user.email_verified is False


async def test_verify_token_unknown(client):
    resp = await client.get("/auth/verify/does-not-exist")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


async def test_verify_token_marked_used_after_first_call(client, db):
    user = await make_user(
        db, email="alice@test.com", username="alice", email_verified=False
    )
    token = EmailToken(
        user_id=user.id,
        token_hash=_token_hash("oneshot-12345"),
        purpose="verify",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(token)
    await db.commit()

    await client.get("/auth/verify/oneshot-12345")
    await db.refresh(token)
    assert token.used_at is not None

    # Second call cannot consume — find_valid_token rejects used tokens.
    from benchlog.auth.tokens import find_valid_token
    second = await find_valid_token(db, "oneshot-12345", "verify")
    assert second is None


# ---------- password reset ----------


async def test_reset_full_flow(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    token = EmailToken(
        user_id=user.id,
        token_hash=_token_hash("reset-me-12345"),
        purpose="reset",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    db.add(token)
    await db.commit()

    # GET should not consume the token
    page = await client.get("/auth/reset/reset-me-12345")
    assert page.status_code == 200

    # POST a new password
    csrf = await csrf_token(client, "/auth/reset/reset-me-12345")
    resp = await client.post(
        "/auth/reset/reset-me-12345",
        data={
            "password": "brand-new-pw",
            "password_confirm": "brand-new-pw",
            "_csrf": csrf,
        },
    )
    assert resp.headers["location"] == "/login"

    # Old password no longer works; new one does
    bad = await login(client, "alice", "testpass1234")
    assert bad.headers["location"] == "/login"
    good = await login(client, "alice", "brand-new-pw")
    assert good.headers["location"] == "/"


async def test_reset_token_single_use(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    token = EmailToken(
        user_id=user.id,
        token_hash=_token_hash("single-reset"),
        purpose="reset",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    db.add(token)
    await db.commit()

    csrf = await csrf_token(client, "/auth/reset/single-reset")
    await client.post(
        "/auth/reset/single-reset",
        data={
            "password": "first-new-pw",
            "password_confirm": "first-new-pw",
            "_csrf": csrf,
        },
    )
    resp2 = await client.post(
        "/auth/reset/single-reset",
        data={
            "password": "second-new-pw",
            "password_confirm": "second-new-pw",
            "_csrf": csrf,
        },
    )
    # Already-consumed token redirects to forgot
    assert resp2.headers["location"] == "/auth/forgot"


async def test_reset_password_mismatch(client, db):
    user = await make_user(db, email="alice@test.com", username="alice")
    token = EmailToken(
        user_id=user.id,
        token_hash=_token_hash("mismatch-token"),
        purpose="reset",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    db.add(token)
    await db.commit()

    csrf = await csrf_token(client, "/auth/reset/mismatch-token")
    resp = await client.post(
        "/auth/reset/mismatch-token",
        data={
            "password": "first-new-pw",
            "password_confirm": "different-pw",
            "_csrf": csrf,
        },
    )
    assert resp.headers["location"] == "/auth/reset/mismatch-token"

    # Token still consumable since the submit failed
    good = await client.post(
        "/auth/reset/mismatch-token",
        data={
            "password": "matching-pw1",
            "password_confirm": "matching-pw1",
            "_csrf": csrf,
        },
    )
    assert good.headers["location"] == "/login"


async def test_forgot_does_not_reveal_account_existence(client, db):
    """Same response whether the email exists or not."""
    db.add(
        SMTPConfig(
            host="smtp.example.com",
            port=587,
            from_address="x@example.com",
            enabled=True,
        )
    )
    await db.commit()
    csrf = await csrf_token(client, "/auth/forgot")
    resp_unknown = await client.post(
        "/auth/forgot", data={"email": "ghost@test.com", "_csrf": csrf}
    )
    await make_user(db, email="real@test.com", username="real")
    resp_known = await client.post(
        "/auth/forgot", data={"email": "real@test.com", "_csrf": csrf}
    )
    assert resp_unknown.status_code == 302
    assert resp_known.status_code == 302
    assert resp_unknown.headers["location"] == resp_known.headers["location"]


async def test_forgot_hidden_when_smtp_not_configured(client, db):
    """Login page omits the forgot-password link and /auth/forgot redirects."""
    await make_user(db, email="real@test.com", username="real")
    login_page = await client.get("/login")
    assert "/auth/forgot" not in login_page.text

    resp = await client.get("/auth/forgot")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"

    csrf = await csrf_token(client, "/login")
    resp = await client.post(
        "/auth/forgot",
        data={"email": "real@test.com", "_csrf": csrf},
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"
