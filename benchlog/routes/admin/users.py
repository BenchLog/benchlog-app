import logging
import uuid

from aiosmtplib.errors import SMTPException
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog import audit
from benchlog.database import get_db
from benchlog.dependencies import require_admin
from benchlog.models import User
from benchlog.models.oidc import OIDCIdentity
from benchlog.auth.passwords import hash_password
from benchlog.auth.users import bump_session_epoch, get_user_by_id
from benchlog.email import get_smtp_config, send_email
from benchlog.site_settings import get_site_settings
from benchlog.templating import templates

logger = logging.getLogger("benchlog.admin")

router = APIRouter()


@router.get("", name="admin_users_list")
async def dashboard(
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User)
        .options(
            selectinload(User.oidc_identities).selectinload(OIDCIdentity.provider),
            selectinload(User.passkeys),
        )
        .order_by(User.created_at)
    )
    users = list(result.scalars().all())
    error = request.session.pop("flash_error", None)
    notice = request.session.pop("flash_notice", None)
    return templates.TemplateResponse(
        request,
        "admin/users.html",
        {"user": admin, "users": users, "error": error, "notice": notice},
    )


@router.post("/{user_id}/toggle-active")
async def toggle_active(
    request: Request,
    user_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    target = await get_user_by_id(db, user_id)
    if target is None:
        raise HTTPException(404)
    if target.id == admin.id:
        request.session["flash_error"] = "You can't disable your own account."
        return RedirectResponse("/admin/users", status_code=302)
    target.is_active = not target.is_active
    # Disabling invalidates active sessions; toggling back doesn't need to.
    if not target.is_active:
        await bump_session_epoch(db, target.id)
    await audit.record(
        db,
        action=audit.ADMIN_USER_DISABLED if not target.is_active else audit.ADMIN_USER_ENABLED,
        request=request,
        actor=admin,
        target_type="user",
        target_id=target.id,
        target_label=f"{target.username} <{target.email}>",
    )
    await db.commit()
    return RedirectResponse("/admin/users", status_code=302)


@router.post("/{user_id}/toggle-admin")
async def toggle_admin(
    request: Request,
    user_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    target = await get_user_by_id(db, user_id)
    if target is None:
        raise HTTPException(404)
    if target.is_site_admin and target.id == admin.id:
        admin_count_result = await db.execute(
            select(func.count(User.id)).where(User.is_site_admin.is_(True))
        )
        if admin_count_result.scalar_one() <= 1:
            request.session["flash_error"] = "Can't demote the last site admin."
            return RedirectResponse("/admin/users", status_code=302)
    was_admin = target.is_site_admin
    target.is_site_admin = not target.is_site_admin
    # Demotion must take effect immediately even on live sessions.
    if was_admin and not target.is_site_admin:
        await bump_session_epoch(db, target.id)
    await audit.record(
        db,
        action=audit.ADMIN_USER_DEMOTED if was_admin else audit.ADMIN_USER_PROMOTED,
        request=request,
        actor=admin,
        target_type="user",
        target_id=target.id,
        target_label=f"{target.username} <{target.email}>",
    )
    await db.commit()
    return RedirectResponse("/admin/users", status_code=302)


@router.post("/{user_id}/reset-password")
async def admin_reset_password(
    request: Request,
    user_id: uuid.UUID,
    new_password: str = Form(...),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    target = await get_user_by_id(db, user_id)
    if target is None:
        raise HTTPException(404)
    if len(new_password) < 8:
        request.session["flash_error"] = "Password must be at least 8 characters."
        return RedirectResponse("/admin/users", status_code=302)
    target.password_hash = hash_password(new_password)
    # Invalidate any active sessions for this user.
    await bump_session_epoch(db, target.id)
    await audit.record(
        db,
        action=audit.ADMIN_USER_PASSWORD_RESET,
        request=request,
        actor=admin,
        target_type="user",
        target_id=target.id,
        target_label=f"{target.username} <{target.email}>",
    )
    await db.commit()

    smtp = await get_smtp_config(db)
    if smtp is not None and smtp.enabled:
        site = await get_site_settings(db)
        try:
            await send_email(
                db,
                to=target.email,
                subject=f"Your {site.site_name} password was reset by an administrator",
                body=(
                    f"The password on your {site.site_name} account was just reset by an administrator.\n\n"
                    "If this wasn't expected, contact your site administrator or sign in with another method "
                    "(linked provider or passkey) and review your connected sign-in methods."
                ),
            )
        except (SMTPException, OSError, RuntimeError):
            logger.warning(
                "Failed to send admin password-reset notification email to %s",
                target.email,
                exc_info=True,
            )

    request.session["flash_notice"] = f"Password reset for {target.username}."
    return RedirectResponse("/admin/users", status_code=302)


@router.post("/{user_id}/delete")
async def delete_user(
    request: Request,
    user_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    target = await get_user_by_id(db, user_id)
    if target is None:
        raise HTTPException(404)
    if target.id == admin.id:
        request.session["flash_error"] = "You can't delete your own account."
        return RedirectResponse("/admin/users", status_code=302)
    await audit.record(
        db,
        action=audit.ADMIN_USER_DELETED,
        request=request,
        actor=admin,
        target_type="user",
        target_id=target.id,
        target_label=f"{target.username} <{target.email}>",
    )
    await db.delete(target)
    await db.commit()
    request.session["flash_notice"] = "User deleted."
    return RedirectResponse("/admin/users", status_code=302)
