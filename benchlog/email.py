from email.message import EmailMessage

import aiosmtplib
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.models import SMTPConfig


async def get_smtp_config(db: AsyncSession) -> SMTPConfig | None:
    result = await db.execute(select(SMTPConfig).limit(1))
    return result.scalar_one_or_none()


async def send_email(db: AsyncSession, to: str, subject: str, body: str) -> None:
    config = await get_smtp_config(db)
    if not config or not config.enabled:
        raise RuntimeError("SMTP is not configured or not enabled")

    message = EmailMessage()
    message["From"] = f"{config.from_name} <{config.from_address}>"
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    await aiosmtplib.send(
        message,
        hostname=config.host,
        port=config.port,
        username=config.username or None,
        password=config.password or None,
        use_tls=config.use_tls and not config.use_starttls,
        start_tls=config.use_starttls,
    )


async def send_test_email(config: SMTPConfig, to: str) -> None:
    """Send a test email without requiring the config to be persisted/enabled."""
    message = EmailMessage()
    message["From"] = f"{config.from_name} <{config.from_address}>"
    message["To"] = to
    message["Subject"] = "BenchLog — SMTP test"
    message.set_content("If you received this, your SMTP configuration is working.")

    await aiosmtplib.send(
        message,
        hostname=config.host,
        port=config.port,
        username=config.username or None,
        password=config.password or None,
        use_tls=config.use_tls and not config.use_starttls,
        start_tls=config.use_starttls,
    )
