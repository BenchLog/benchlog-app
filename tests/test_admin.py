"""Admin user-management self-protection rules + settings save."""

from sqlalchemy import select

from benchlog.models import OIDCProvider, SiteSettings, SMTPConfig, User
from benchlog.auth.passwords import verify_password
from tests.conftest import csrf_token, login, make_user


async def _token(client):
    return await csrf_token(client, "/admin/users")


# ---------- toggle-active ----------


async def test_admin_can_disable_other_user(client, db):
    await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    target = await make_user(db, email="bob@test.com", username="bob")
    await login(client, "admin")

    resp = await client.post(
        f"/admin/users/{target.id}/toggle-active",
        data={"_csrf": await _token(client)},
    )
    assert resp.status_code == 302

    await db.refresh(target)
    assert target.is_active is False


async def test_admin_cannot_disable_themselves(client, db):
    admin = await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    await login(client, "admin")

    resp = await client.post(
        f"/admin/users/{admin.id}/toggle-active",
        data={"_csrf": await _token(client)},
    )
    assert resp.headers["location"] == "/admin/users"

    await db.refresh(admin)
    assert admin.is_active is True


# ---------- toggle-admin ----------


async def test_admin_can_promote_user(client, db):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    target = await make_user(db, email="bob@test.com", username="bob")
    await login(client, "admin")

    await client.post(
        f"/admin/users/{target.id}/toggle-admin",
        data={"_csrf": await _token(client)},
    )
    await db.refresh(target)
    assert target.is_site_admin is True


async def test_cannot_demote_last_admin(client, db):
    sole_admin = await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    await login(client, "admin")

    resp = await client.post(
        f"/admin/users/{sole_admin.id}/toggle-admin",
        data={"_csrf": await _token(client)},
    )
    assert resp.headers["location"] == "/admin/users"

    await db.refresh(sole_admin)
    assert sole_admin.is_site_admin is True


async def test_admin_can_demote_self_when_other_admin_exists(client, db):
    admin1 = await make_user(
        db, email="admin1@test.com", username="admin1", is_site_admin=True
    )
    await make_user(
        db, email="admin2@test.com", username="admin2", is_site_admin=True
    )
    await login(client, "admin1")

    await client.post(
        f"/admin/users/{admin1.id}/toggle-admin",
        data={"_csrf": await _token(client)},
    )
    await db.refresh(admin1)
    assert admin1.is_site_admin is False


# ---------- reset-password ----------


async def test_admin_resets_user_password(client, db):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    target = await make_user(db, email="bob@test.com", username="bob")
    await login(client, "admin")

    await client.post(
        f"/admin/users/{target.id}/reset-password",
        data={"new_password": "freshly-reset-pw", "_csrf": await _token(client)},
    )
    await db.refresh(target)
    assert verify_password("freshly-reset-pw", target.password_hash) is True


async def test_admin_reset_too_short_rejected(client, db):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    target = await make_user(db, email="bob@test.com", username="bob")
    await login(client, "admin")

    await client.post(
        f"/admin/users/{target.id}/reset-password",
        data={"new_password": "short", "_csrf": await _token(client)},
    )
    await db.refresh(target)
    assert verify_password("testpass1234", target.password_hash) is True


# ---------- delete ----------


async def test_admin_can_delete_other_user(client, db):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    target = await make_user(db, email="bob@test.com", username="bob")
    await login(client, "admin")

    await client.post(
        f"/admin/users/{target.id}/delete",
        data={"_csrf": await _token(client)},
    )

    remaining = (
        await db.execute(select(User).where(User.id == target.id))
    ).scalar_one_or_none()
    assert remaining is None


async def test_admin_cannot_delete_themselves(client, db):
    admin = await make_user(
        db, email="admin@test.com", username="admin", is_site_admin=True
    )
    await login(client, "admin")

    resp = await client.post(
        f"/admin/users/{admin.id}/delete",
        data={"_csrf": await _token(client)},
    )
    assert resp.headers["location"] == "/admin/users"

    still = (
        await db.execute(select(User).where(User.id == admin.id))
    ).scalar_one_or_none()
    assert still is not None


# ---------- site settings ----------


async def test_admin_saves_site_settings(client, db):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    await login(client, "admin")

    resp = await client.post(
        "/admin/settings/save",
        data={
            "site_name": "My BenchLog",
            "allow_local_signup": "on",
            "require_email_verification": "on",
            "_csrf": await _token(client),
        },
    )
    assert resp.headers["location"] == "/admin/settings"

    site = (await db.execute(select(SiteSettings))).scalar_one()
    assert site.site_name == "My BenchLog"
    assert site.allow_local_signup is True
    assert site.require_email_verification is True


async def test_settings_unchecked_boxes_become_false(client, db):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    db.add(
        SiteSettings(
            site_name="X",
            allow_local_signup=True,
            require_email_verification=True,
        )
    )
    await db.commit()
    await login(client, "admin")

    await client.post(
        "/admin/settings/save",
        data={"site_name": "Y", "_csrf": await _token(client)},
    )
    site = (await db.execute(select(SiteSettings))).scalar_one()
    assert site.allow_local_signup is False
    assert site.require_email_verification is False


# ---------- SMTP config ----------


async def test_admin_saves_smtp_config(client, db):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    await login(client, "admin")

    resp = await client.post(
        "/admin/smtp/save",
        data={
            "host": "smtp.example.com",
            "port": "587",
            "username": "u",
            "password": "p",
            "from_address": "noreply@example.com",
            "from_name": "BenchLog",
            "use_starttls": "on",
            "enabled": "on",
            "_csrf": await _token(client),
        },
    )
    assert resp.headers["location"] == "/admin/smtp"

    config = (await db.execute(select(SMTPConfig))).scalar_one()
    assert config.host == "smtp.example.com"
    assert config.port == 587
    assert config.use_starttls is True
    assert config.enabled is True
    assert config.password == "p"


async def test_smtp_password_unchanged_when_blank(client, db):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    db.add(
        SMTPConfig(
            host="smtp.example.com",
            port=587,
            from_address="x@example.com",
            password="existing-secret",
        )
    )
    await db.commit()
    await login(client, "admin")

    await client.post(
        "/admin/smtp/save",
        data={
            "host": "smtp.example.com",
            "port": "465",
            "from_address": "x@example.com",
            "from_name": "BenchLog",
            "password": "",
            "_csrf": await _token(client),
        },
    )
    config = (await db.execute(select(SMTPConfig))).scalar_one()
    assert config.password == "existing-secret"
    assert config.port == 465


# ---------- OIDC providers ----------


async def test_admin_creates_oidc_provider(client, db):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    await login(client, "admin")

    resp = await client.post(
        "/admin/oidc/save",
        data={
            "id": "",
            "slug": "google",
            "display_name": "Google",
            "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
            "client_id": "cid",
            "client_secret": "csecret",
            "scopes": "openid email profile",
            "enabled": "on",
            "auto_create_users": "on",
            "_csrf": await _token(client),
        },
    )
    assert resp.headers["location"] == "/admin/oidc"

    provider = (await db.execute(select(OIDCProvider))).scalar_one()
    assert provider.slug == "google"
    assert provider.enabled is True
    assert provider.auto_create_users is True
    assert provider.auto_link_verified_email is False


async def test_admin_oidc_duplicate_slug_rejected(client, db):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    db.add(
        OIDCProvider(
            slug="google",
            display_name="Google",
            discovery_url="https://example.com",
            client_id="cid",
            client_secret="csecret",
        )
    )
    await db.commit()
    await login(client, "admin")

    resp = await client.post(
        "/admin/oidc/save",
        data={
            "id": "",
            "slug": "google",
            "display_name": "Other",
            "discovery_url": "https://other.example.com",
            "client_id": "cid2",
            "client_secret": "secret2",
            "_csrf": await _token(client),
        },
    )
    assert resp.headers["location"] == "/admin/oidc/new"

    providers = (await db.execute(select(OIDCProvider))).scalars().all()
    assert len(providers) == 1


async def test_admin_oidc_secret_unchanged_when_blank(client, db):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    provider = OIDCProvider(
        slug="google",
        display_name="Google",
        discovery_url="https://example.com",
        client_id="cid",
        client_secret="original-secret",
    )
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    await login(client, "admin")

    await client.post(
        "/admin/oidc/save",
        data={
            "id": str(provider.id),
            "slug": "google",
            "display_name": "Google",
            "discovery_url": "https://example.com",
            "client_id": "cid",
            "client_secret": "",
            "_csrf": await _token(client),
        },
    )
    await db.refresh(provider)
    assert provider.client_secret == "original-secret"


async def test_admin_deletes_oidc_provider(client, db):
    await make_user(db, email="admin@test.com", username="admin", is_site_admin=True)
    provider = OIDCProvider(
        slug="google",
        display_name="Google",
        discovery_url="https://example.com",
        client_id="cid",
        client_secret="secret",
    )
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    pid = provider.id
    await login(client, "admin")

    await client.post(
        f"/admin/oidc/{pid}/delete",
        data={"_csrf": await _token(client)},
    )
    remaining = (
        await db.execute(select(OIDCProvider).where(OIDCProvider.id == pid))
    ).scalar_one_or_none()
    assert remaining is None
