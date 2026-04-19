"""Seed runtime config from env vars on startup.

Runs once during the FastAPI `lifespan` hook (see main.py). Idempotent:
skips seeding when a row already exists, so restarting with unchanged env
vars is a no-op. Changing values after first seed won't update the DB —
edit via the admin UI instead (or wipe the row and restart).
"""
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.config import Settings
from benchlog.models import OIDCProvider, SMTPConfig

logger = logging.getLogger("benchlog.bootstrap")


def _clean_slug(value: str) -> str:
    return "".join(ch for ch in value.strip().lower() if ch.isalnum() or ch in "-_")


async def seed_initial_smtp(db: AsyncSession, settings: Settings) -> bool:
    """Seed SMTPConfig from env vars when no usable config exists.

    Runs if there's no row yet OR the current row has a blank host
    (i.e. admin cleared the values). Returns True if a row was written.
    """
    if not settings.initial_smtp_host.strip():
        return False

    result = await db.execute(select(SMTPConfig).limit(1))
    config = result.scalar_one_or_none()

    if config is not None and config.host.strip():
        return False

    if config is None:
        config = SMTPConfig()
        db.add(config)

    config.host = settings.initial_smtp_host.strip()
    config.port = settings.initial_smtp_port
    config.username = settings.initial_smtp_username.strip()
    config.password = settings.initial_smtp_password
    config.from_address = settings.initial_smtp_from_address.strip()
    config.from_name = settings.initial_smtp_from_name.strip() or "BenchLog"
    config.use_tls = settings.initial_smtp_use_tls
    config.use_starttls = settings.initial_smtp_use_starttls
    config.enabled = settings.initial_smtp_enabled
    await db.commit()
    logger.info("Seeded initial SMTP config from env (host=%s)", config.host)
    return True


async def seed_initial_oidc(db: AsyncSession, settings: Settings) -> bool:
    """Seed a single OIDCProvider from env vars when no providers exist.

    Requires slug, discovery URL, and client ID to be set. Returns True if
    a provider was created.
    """
    slug = _clean_slug(settings.initial_oidc_slug)
    discovery_url = settings.initial_oidc_discovery_url.strip()
    client_id = settings.initial_oidc_client_id.strip()
    if not slug or not discovery_url or not client_id:
        return False

    count = await db.scalar(select(func.count()).select_from(OIDCProvider))
    if count and count > 0:
        return False

    provider = OIDCProvider(
        slug=slug,
        display_name=settings.initial_oidc_display_name.strip() or slug,
        discovery_url=discovery_url,
        client_id=client_id,
        client_secret=settings.initial_oidc_client_secret,
        scopes=settings.initial_oidc_scopes.strip() or "openid email profile",
        enabled=settings.initial_oidc_enabled,
        auto_create_users=settings.initial_oidc_auto_create_users,
        auto_link_verified_email=settings.initial_oidc_auto_link_verified_email,
        allow_private_network=settings.initial_oidc_allow_private_network,
    )
    db.add(provider)
    await db.commit()
    logger.info("Seeded initial OIDC provider from env (slug=%s)", slug)
    return True


async def seed_initial_config(db: AsyncSession, settings: Settings) -> None:
    await seed_initial_smtp(db, settings)
    await seed_initial_oidc(db, settings)
