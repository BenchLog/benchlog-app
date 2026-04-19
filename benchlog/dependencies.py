import uuid

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.database import get_db
from benchlog.models import User
from benchlog.auth.users import get_user_by_id


async def current_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User | None:
    session_user = request.session.get("user")
    if not session_user:
        return None
    try:
        user_id = uuid.UUID(session_user["id"])
        session_epoch = int(session_user.get("epoch", 0))
    except (KeyError, ValueError, TypeError):
        return None
    user = await get_user_by_id(db, user_id)
    if user is None or not user.is_active:
        return None
    # Epoch mismatch = session was invalidated (password change, admin action).
    if user.session_epoch != session_epoch:
        request.session.pop("user", None)
        return None
    return user


async def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


async def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_site_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return user
