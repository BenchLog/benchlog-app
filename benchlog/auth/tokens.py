import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.models import EmailToken


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class CreatedToken:
    """Returned by create_email_token. `plaintext` is only available here."""
    id: uuid.UUID
    plaintext: str


async def invalidate_user_tokens(
    db: AsyncSession, user_id: uuid.UUID, purpose: str
) -> None:
    """Mark all unused tokens for (user, purpose) as used.

    Called before issuing a replacement token so prior emailed links stop
    working — otherwise a user who resends would have multiple valid links
    floating around, and a leaked older link would remain usable until TTL.
    """
    await db.execute(
        update(EmailToken)
        .where(
            EmailToken.user_id == user_id,
            EmailToken.purpose == purpose,
            EmailToken.used_at.is_(None),
        )
        .values(used_at=_now())
    )


async def create_email_token(
    db: AsyncSession, user_id: uuid.UUID, purpose: str, ttl_hours: int = 24
) -> CreatedToken:
    plaintext = secrets.token_urlsafe(48)
    row = EmailToken(
        user_id=user_id,
        token_hash=_hash(plaintext),
        purpose=purpose,
        expires_at=_now() + timedelta(hours=ttl_hours),
    )
    db.add(row)
    await db.flush()
    return CreatedToken(id=row.id, plaintext=plaintext)


async def find_valid_token(
    db: AsyncSession, token: str, purpose: str
) -> EmailToken | None:
    """Return the token if present, unused and unexpired. Does not mutate."""
    from sqlalchemy import select

    result = await db.execute(
        select(EmailToken).where(
            EmailToken.token_hash == _hash(token),
            EmailToken.purpose == purpose,
        )
    )
    record = result.scalar_one_or_none()
    if record is None or record.used_at is not None or record.expires_at < _now():
        return None
    return record


async def consume_email_token(
    db: AsyncSession, token: str, purpose: str
) -> EmailToken | None:
    """Atomically mark token used. Returns the row if consumed, else None.

    The UPDATE ... WHERE used_at IS NULL AND expires_at > now() guarantees only
    one concurrent caller can consume a given token.
    """
    now = _now()
    stmt = (
        update(EmailToken)
        .where(
            EmailToken.token_hash == _hash(token),
            EmailToken.purpose == purpose,
            EmailToken.used_at.is_(None),
            EmailToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(EmailToken)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()
