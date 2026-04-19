import logging
import uuid

from aiosmtplib.errors import SMTPException
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog import audit
from benchlog.config import settings
from benchlog.database import get_db
from benchlog.dependencies import require_user
from benchlog.models import OIDCIdentity, User, WebAuthnCredential
from benchlog.auth import oidc as oidc_svc
from benchlog.auth import users as user_svc
from benchlog.auth.passwords import hash_password, verify_password
from benchlog.auth.signup import (
    SignupValidationError,
    validate_profile_fields,
)
from benchlog.auth.tokens import create_email_token, invalidate_user_tokens
from benchlog.email import get_smtp_config, send_email
from benchlog.site_settings import get_site_settings
from benchlog.templating import templates

logger = logging.getLogger("benchlog.account")

router = APIRouter(prefix="/account")


@router.get("")
async def account_page(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    identities_result = await db.execute(
        select(OIDCIdentity)
        .where(OIDCIdentity.user_id == user.id)
        .options(selectinload(OIDCIdentity.provider))
    )
    identities = list(identities_result.scalars().all())
    linked_provider_ids = {i.provider_id for i in identities}

    all_providers = await oidc_svc.get_enabled_providers(db)
    unlinked_providers = [p for p in all_providers if p.id not in linked_provider_ids]

    passkeys_result = await db.execute(
        select(WebAuthnCredential)
        .where(WebAuthnCredential.user_id == user.id)
        .order_by(WebAuthnCredential.created_at)
    )
    passkeys = list(passkeys_result.scalars().all())

    has_password = user.password_hash is not None

    error = request.session.pop("flash_error", None)
    notice = request.session.pop("flash_notice", None)

    return templates.TemplateResponse(
        request,
        "account/settings.html",
        {
            "user": user,
            "identities": identities,
            "unlinked_providers": unlinked_providers,
            "passkeys": passkeys,
            "has_password": has_password,
            "error": error,
            "notice": notice,
        },
    )


@router.post("/profile")
async def update_profile(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        fields = await validate_profile_fields(
            db,
            user_id=user.id,
            email=email,
            display_name=display_name,
        )
    except SignupValidationError as exc:
        request.session["flash_error"] = str(exc)
        return RedirectResponse("/account", status_code=302)

    new_display_name = fields.display_name or user.display_name
    current_email = user.email
    email_changed = fields.email.lower() != current_email.lower()

    if not email_changed:
        display_changed = new_display_name != user.display_name
        user.display_name = new_display_name

        # Submitting the current email while a change was pending cancels it.
        canceled_pending = user.pending_email
        if canceled_pending is not None:
            await invalidate_user_tokens(db, user.id, purpose="verify")
            user.pending_email = None
            await audit.record(
                db,
                action=audit.AUTH_EMAIL_CHANGE_CANCELED,
                request=request,
                actor=user,
                metadata={"canceled_email": canceled_pending},
            )

        if display_changed:
            await audit.record(
                db,
                action=audit.ACCOUNT_PROFILE_UPDATED,
                request=request,
                actor=user,
                metadata={"changed": ["display_name"]},
            )
        await db.commit()
        if canceled_pending is not None:
            request.session["flash_notice"] = "Pending email change canceled."
        else:
            request.session["flash_notice"] = "Profile updated."
        return RedirectResponse("/account", status_code=302)

    site = await get_site_settings(db)
    smtp = await get_smtp_config(db)
    smtp_ready = smtp is not None and smtp.enabled

    # Email change requires SMTP — there's no way to verify the new address
    # without sending a link to it.
    if not smtp_ready:
        request.session["flash_error"] = (
            "Email changes require email delivery to be configured. "
            "Contact your site administrator."
        )
        return RedirectResponse("/account", status_code=302)

    user.display_name = new_display_name
    user.pending_email = fields.email

    # Invalidate any prior pending-change tokens before issuing a new one, so a
    # stale link from a previous attempt can no longer verify a different address.
    await invalidate_user_tokens(db, user.id, purpose="verify")
    token = await create_email_token(db, user.id, purpose="verify")

    await audit.record(
        db,
        action=audit.AUTH_EMAIL_CHANGE_REQUESTED,
        request=request,
        actor=user,
        metadata={"current_email": current_email, "pending_email": user.pending_email},
    )
    await db.commit()

    try:
        await send_email(
            db,
            to=current_email,
            subject=f"A change to your {site.site_name} email was requested",
            body=(
                f"A request was made to change your {site.site_name} email address "
                f"from {current_email} to {user.pending_email}.\n\n"
                f"The change will only take effect once it's confirmed from the new "
                f"address. Until then, your account continues to use {current_email}.\n\n"
                "If this wasn't you, no action is required — the change has not taken "
                "effect. You may also sign in and request a different email to cancel "
                "the pending change."
            ),
        )
    except (SMTPException, OSError, RuntimeError):
        logger.warning(
            "Failed to send email-change notification to current address %s",
            current_email,
            exc_info=True,
        )

    verify_url = f"{settings.base_url}/auth/verify/{token.plaintext}"
    try:
        await send_email(
            db,
            to=user.pending_email,
            subject=f"Confirm your new email for {site.site_name}",
            body=(
                f"Confirm this address as the new email for your {site.site_name} "
                f"account:\n\n{verify_url}\n\n"
                "This link expires in 24 hours. Until you confirm, your account "
                f"continues to use {current_email}."
            ),
        )
    except (SMTPException, OSError, RuntimeError):
        logger.warning(
            "Failed to send email-change verification to %s",
            user.pending_email,
            exc_info=True,
        )

    request.session["flash_notice"] = (
        f"Check {user.pending_email} for a verification link. Your account "
        f"will keep using {current_email} until the new address is confirmed."
    )
    return RedirectResponse("/account", status_code=302)


@router.post("/password")
async def change_password(
    request: Request,
    current_password: str = Form(""),
    password: str = Form(...),
    password_confirm: str = Form(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    had_password = user.password_hash is not None
    if had_password:
        if not current_password or not verify_password(current_password, user.password_hash):
            request.session["flash_error"] = "Current password is incorrect."
            return RedirectResponse("/account", status_code=302)
    if password != password_confirm:
        request.session["flash_error"] = "Passwords do not match."
        return RedirectResponse("/account", status_code=302)
    if len(password) < 8:
        request.session["flash_error"] = "Password must be at least 8 characters."
        return RedirectResponse("/account", status_code=302)
    user.password_hash = hash_password(password)
    # Invalidate any other sessions for this user. Re-stamp current session.
    new_epoch = await user_svc.bump_session_epoch(db, user.id)
    await audit.record(
        db,
        action=audit.AUTH_PASSWORD_CHANGED,
        request=request,
        actor=user,
        metadata={"first_set": not had_password},
    )
    await db.commit()
    request.session["user"] = {"id": str(user.id), "epoch": new_epoch}

    smtp = await get_smtp_config(db)
    if smtp is not None and smtp.enabled:
        site = await get_site_settings(db)
        action = "changed" if had_password else "set"
        try:
            await send_email(
                db,
                to=user.email,
                subject=f"Your {site.site_name} password was {action}",
                body=(
                    f"The password on your {site.site_name} account was just {action}.\n\n"
                    "If this wasn't you, sign in with another method and remove the password "
                    "immediately, then review your connected sign-in methods."
                ),
            )
        except (SMTPException, OSError, RuntimeError):
            logger.warning(
                "Failed to send password-change notification email to %s",
                user.email,
                exc_info=True,
            )

    request.session["flash_notice"] = "Password updated."
    return RedirectResponse("/account", status_code=302)


@router.post("/password/delete")
async def delete_password(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if user.password_hash is None:
        request.session["flash_error"] = "No password set."
        return RedirectResponse("/account", status_code=302)

    if not await user_svc.other_sign_in_methods_exist(
        db, user, pretend_no_password=True
    ):
        request.session["flash_error"] = (
            "Can't remove your password — it's your only sign-in method. Link a provider or add a passkey first."
        )
        return RedirectResponse("/account", status_code=302)

    user.password_hash = None
    new_epoch = await user_svc.bump_session_epoch(db, user.id)
    await audit.record(
        db, action=audit.AUTH_PASSWORD_REMOVED, request=request, actor=user
    )
    await db.commit()
    request.session["user"] = {"id": str(user.id), "epoch": new_epoch}
    request.session["flash_notice"] = "Password removed."
    return RedirectResponse("/account", status_code=302)


@router.post("/delete")
async def delete_account(
    request: Request,
    confirm_username: str = Form(""),
    current_password: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if confirm_username.strip().lower() != user.username.lower():
        request.session["flash_error"] = (
            "Type your username exactly to confirm account deletion."
        )
        return RedirectResponse("/account", status_code=302)

    if user.password_hash is not None:
        if not current_password or not verify_password(
            current_password, user.password_hash
        ):
            request.session["flash_error"] = "Current password is incorrect."
            return RedirectResponse("/account", status_code=302)

    if user.is_site_admin:
        admin_count = (
            await db.execute(
                select(func.count(User.id)).where(User.is_site_admin.is_(True))
            )
        ).scalar_one()
        if admin_count <= 1:
            request.session["flash_error"] = (
                "You're the last site admin — promote someone else before deleting your account."
            )
            return RedirectResponse("/account", status_code=302)

    deleted_email = user.email
    deleted_username = user.username
    deleted_user_id = user.id
    site = await get_site_settings(db)
    smtp = await get_smtp_config(db)
    smtp_ready = smtp is not None and smtp.enabled

    # Record before delete: actor_user_id FK is SET NULL but actor_label
    # preserves who did this for the audit trail.
    await audit.record(
        db,
        action=audit.ACCOUNT_DELETED,
        request=request,
        actor=user,
        target_type="user",
        target_id=deleted_user_id,
        target_label=f"{deleted_username} <{deleted_email}>",
    )
    await db.delete(user)
    await db.commit()

    if smtp_ready:
        try:
            await send_email(
                db,
                to=deleted_email,
                subject=f"Your {site.site_name} account was deleted",
                body=(
                    f"Your {site.site_name} account ({deleted_email}) was just deleted.\n\n"
                    "If this wasn't you, contact your site administrator immediately."
                ),
            )
        except (SMTPException, OSError, RuntimeError):
            logger.warning(
                "Failed to send account-deletion notification email to %s",
                deleted_email,
                exc_info=True,
            )

    request.session.clear()
    request.session["flash_notice"] = "Your account has been deleted."
    return RedirectResponse("/login", status_code=302)


@router.post("/oidc/{identity_id}/unlink")
async def unlink_oidc(
    request: Request,
    identity_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(OIDCIdentity)
        .where(OIDCIdentity.id == identity_id, OIDCIdentity.user_id == user.id)
        .options(selectinload(OIDCIdentity.provider))
    )
    identity = result.scalar_one_or_none()
    if identity is None:
        request.session["flash_error"] = "Link not found."
        return RedirectResponse("/account", status_code=302)

    if not await user_svc.other_sign_in_methods_exist(
        db, user, excluding_oidc_id=identity_id
    ):
        request.session["flash_error"] = (
            "Can't unlink — it's your only sign-in method. Set a password or add a passkey first."
        )
        return RedirectResponse("/account", status_code=302)

    provider_label = identity.provider.display_name if identity.provider else None
    await audit.record(
        db,
        action=audit.AUTH_OIDC_UNLINKED,
        request=request,
        actor=user,
        target_type="oidc_identity",
        target_id=identity.id,
        target_label=provider_label,
    )
    await db.delete(identity)
    await db.commit()
    request.session["flash_notice"] = "Sign-in method unlinked."
    return RedirectResponse("/account", status_code=302)
