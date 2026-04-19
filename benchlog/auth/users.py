import uuid

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.models import OIDCIdentity, User, WebAuthnCredential


async def user_count(db: AsyncSession) -> int:
    result = await db.execute(select(func.count(User.id)))
    return result.scalar_one()


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(func.lower(User.email) == email.lower()))
    return result.scalar_one_or_none()


async def get_user_by_email_or_pending(db: AsyncSession, email: str) -> User | None:
    """Find any user whose verified email OR pending_email matches.

    Used for collision checks on signup and email change: we must reject an
    address that is either already in use OR currently mid-verification for
    someone else, otherwise the other user's pending change would fail when
    they click their verification link.
    """
    lowered = email.lower()
    result = await db.execute(
        select(User).where(
            or_(
                func.lower(User.email) == lowered,
                func.lower(User.pending_email) == lowered,
            )
        )
    )
    return result.scalars().first()


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(
        select(User).where(func.lower(User.username) == username.lower())
    )
    return result.scalar_one_or_none()


async def get_user_by_login(db: AsyncSession, identifier: str) -> User | None:
    """Look up by email if it contains '@', otherwise by username."""
    if "@" in identifier:
        return await get_user_by_email(db, identifier)
    return await get_user_by_username(db, identifier)


async def other_sign_in_methods_exist(
    db: AsyncSession,
    user: User,
    *,
    excluding_oidc_id: uuid.UUID | None = None,
    excluding_passkey_id: uuid.UUID | None = None,
    pretend_no_password: bool = False,
) -> bool:
    """Does the user have at least one sign-in method other than the one being removed?

    Pass pretend_no_password=True when the caller is removing the password.
    Pass excluding_* when unlinking a specific OIDC identity or passkey.
    """
    if user.password_hash is not None and not pretend_no_password:
        return True

    oidc_q = select(func.count(OIDCIdentity.id)).where(OIDCIdentity.user_id == user.id)
    if excluding_oidc_id is not None:
        oidc_q = oidc_q.where(OIDCIdentity.id != excluding_oidc_id)
    if (await db.execute(oidc_q)).scalar_one() > 0:
        return True

    pk_q = select(func.count(WebAuthnCredential.id)).where(
        WebAuthnCredential.user_id == user.id
    )
    if excluding_passkey_id is not None:
        pk_q = pk_q.where(WebAuthnCredential.id != excluding_passkey_id)
    return (await db.execute(pk_q)).scalar_one() > 0


async def bump_session_epoch(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Invalidate all existing sessions for this user. Returns the new epoch."""
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(session_epoch=User.session_epoch + 1)
        .returning(User.session_epoch)
    )
    result = await db.execute(stmt)
    return result.scalar_one()
