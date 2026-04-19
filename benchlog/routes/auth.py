import logging

from aiosmtplib.errors import SMTPException
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog import audit
from benchlog.config import settings
from benchlog.database import get_db
from benchlog.dependencies import current_user
from benchlog.models import User
from benchlog.auth import oidc as oidc_svc
from benchlog.auth import users as user_svc
from benchlog.auth.passwords import dummy_verify, hash_password, verify_password
from benchlog.auth.signup import (
    EmailAlreadyRegistered,
    SignupValidationError,
    validate_signup_fields,
)
from benchlog.auth.tokens import (
    consume_email_token,
    create_email_token,
    find_valid_token,
    invalidate_user_tokens,
)
from benchlog.email import get_smtp_config, send_email
from benchlog.rate_limit import rate_limit
from benchlog.site_settings import get_site_settings
from benchlog.templating import templates

logger = logging.getLogger("benchlog.auth")

router = APIRouter()


def _set_session(request: Request, user: User) -> None:
    """Set the session for a newly authenticated user.

    Clear any pre-login state (CSRF, flash, in-flight OIDC/passkey/signup
    challenges) to avoid fixation and stale-challenge issues, then stamp the
    user id and epoch. Epoch mismatch in current_user will invalidate stale
    sessions after a password change / admin action.
    """
    request.session.clear()
    request.session["user"] = {"id": str(user.id), "epoch": user.session_epoch}


@router.get("/login")
async def login_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if user:
        return RedirectResponse("/", status_code=302)
    site = await get_site_settings(db)
    providers = await oidc_svc.get_enabled_providers(db)
    error = request.session.pop("flash_error", None)
    notice = request.session.pop("flash_notice", None)
    show_resend = request.session.pop("flash_show_resend", False)
    total_users = await user_svc.user_count(db)
    smtp = await get_smtp_config(db)
    email_enabled = smtp is not None and smtp.enabled
    return templates.TemplateResponse(
        request,
        "auth/login.html",
        {
            "site": site,
            "providers": providers,
            "error": error,
            "notice": notice,
            "show_resend": show_resend and email_enabled,
            "email_enabled": email_enabled,
            "first_run": total_users == 0,
            "allow_signup": site.allow_local_signup or total_users == 0,
        },
    )


@router.post("/login", dependencies=[Depends(rate_limit("login", 10, 60))])
async def login_submit(
    request: Request,
    identifier: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    identifier = identifier.strip()
    user = await user_svc.get_user_by_login(db, identifier)
    if user is None or user.password_hash is None:
        # Perform an equivalent bcrypt cost so response timing doesn't leak
        # whether the identifier matched a user with a password set.
        dummy_verify()
        await audit.record(
            db,
            action=audit.AUTH_LOGIN_FAILED,
            request=request,
            outcome="failure",
            actor_label=identifier[:256] or None,
            metadata={"reason": "no_user_or_no_password"},
        )
        await db.commit()
        request.session["flash_error"] = "Invalid credentials."
        return RedirectResponse("/login", status_code=302)
    if not verify_password(password, user.password_hash):
        await audit.record(
            db,
            action=audit.AUTH_LOGIN_FAILED,
            request=request,
            actor=user,
            outcome="failure",
            metadata={"reason": "bad_password"},
        )
        await db.commit()
        request.session["flash_error"] = "Invalid credentials."
        return RedirectResponse("/login", status_code=302)
    if not user.is_active:
        await audit.record(
            db,
            action=audit.AUTH_LOGIN_BLOCKED_DISABLED,
            request=request,
            actor=user,
            outcome="failure",
        )
        await db.commit()
        request.session["flash_error"] = "Your account is disabled."
        return RedirectResponse("/login", status_code=302)

    site = await get_site_settings(db)
    if site.require_email_verification and not user.email_verified:
        await audit.record(
            db,
            action=audit.AUTH_LOGIN_BLOCKED_UNVERIFIED,
            request=request,
            actor=user,
            outcome="failure",
        )
        await db.commit()
        request.session["flash_error"] = (
            "Please verify your email before logging in. Check your inbox for a verification link."
        )
        request.session["flash_show_resend"] = True
        return RedirectResponse("/login", status_code=302)

    await audit.record(
        db,
        action=audit.AUTH_LOGIN_SUCCESS,
        request=request,
        actor=user,
        metadata={"method": "password"},
    )
    await db.commit()
    _set_session(request, user)
    return RedirectResponse("/", status_code=302)


@router.get("/signup")
async def signup_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if user:
        return RedirectResponse("/", status_code=302)
    site = await get_site_settings(db)
    total_users = await user_svc.user_count(db)
    if not site.allow_local_signup and total_users > 0:
        request.session["flash_error"] = "Local signup is disabled."
        return RedirectResponse("/login", status_code=302)
    error = request.session.pop("flash_error", None)
    return templates.TemplateResponse(
        request,
        "auth/signup.html",
        {"site": site, "first_run": total_users == 0, "error": error},
    )


@router.post("/signup", dependencies=[Depends(rate_limit("signup", 5, 300))])
async def signup_submit(
    request: Request,
    email: str = Form(...),
    username: str = Form(...),
    display_name: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    site = await get_site_settings(db)
    total_users = await user_svc.user_count(db)
    first_run = total_users == 0

    if not first_run and not site.allow_local_signup:
        request.session["flash_error"] = "Local signup is disabled."
        return RedirectResponse("/login", status_code=302)

    if password != password_confirm:
        request.session["flash_error"] = "Passwords do not match."
        return RedirectResponse("/signup", status_code=302)
    if len(password) < 8:
        request.session["flash_error"] = "Password must be at least 8 characters."
        return RedirectResponse("/signup", status_code=302)

    try:
        fields = await validate_signup_fields(db, email, username, display_name)
    except EmailAlreadyRegistered:
        # Don't reveal that this email is registered. Respond exactly as we
        # would for a successful signup that requires email verification —
        # the attacker sees the same flash + redirect either way.
        await db.rollback()
        request.session["flash_notice"] = (
            "Account created. Check your email for a verification link before logging in."
        )
        return RedirectResponse("/login", status_code=302)
    except SignupValidationError as exc:
        request.session["flash_error"] = str(exc)
        return RedirectResponse("/signup", status_code=302)

    user = User(
        email=fields.email,
        username=fields.username,
        display_name=fields.display_name,
        password_hash=hash_password(password),
        is_site_admin=first_run,
        email_verified=first_run,
    )
    db.add(user)
    await db.flush()

    requires_verification = site.require_email_verification and not first_run
    smtp = await get_smtp_config(db)
    smtp_ready = smtp is not None and smtp.enabled

    if requires_verification and smtp_ready:
        token = await create_email_token(db, user.id, purpose="verify")
        await audit.record(
            db,
            action=audit.AUTH_SIGNUP,
            request=request,
            actor=user,
            metadata={
                "method": "password",
                "first_run": first_run,
                "verification_required": True,
            },
        )
        await db.commit()
        verify_url = f"{settings.base_url}/auth/verify/{token.plaintext}"
        try:
            await send_email(
                db,
                to=user.email,
                subject=f"Verify your email for {site.site_name}",
                body=f"Welcome to {site.site_name}! Confirm your email here:\n\n{verify_url}\n\nThis link expires in 24 hours.",
            )
        except (SMTPException, OSError, RuntimeError):
            logger.warning(
                "Failed to send signup verification email to %s", user.email, exc_info=True
            )
        request.session["flash_notice"] = (
            "Account created. Check your email for a verification link before logging in."
        )
        return RedirectResponse("/login", status_code=302)

    await audit.record(
        db,
        action=audit.AUTH_SIGNUP,
        request=request,
        actor=user,
        metadata={"method": "password", "first_run": first_run},
    )
    await db.commit()
    _set_session(request, user)
    return RedirectResponse("/", status_code=302)


@router.post("/logout")
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if user is not None:
        await audit.record(db, action=audit.AUTH_LOGOUT, request=request, actor=user)
        await db.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@router.get("/auth/verify/{token}")
async def verify_email(
    request: Request, token: str, db: AsyncSession = Depends(get_db)
):
    record = await consume_email_token(db, token, purpose="verify")
    if record is None:
        request.session["flash_error"] = "Verification link is invalid or expired."
        return RedirectResponse("/login", status_code=302)
    user = await user_svc.get_user_by_id(db, record.user_id)
    if user is None:
        request.session["flash_error"] = "Verification link is invalid."
        return RedirectResponse("/login", status_code=302)

    pending = user.pending_email
    if pending is not None:
        # Confirming a requested email change: re-check the address isn't in
        # use now (someone else may have claimed it since the change was
        # requested) before swapping it in for the verified email.
        collision = await user_svc.get_user_by_email(db, pending)
        if collision is not None and collision.id != user.id:
            user.pending_email = None
            await db.commit()
            request.session["flash_error"] = (
                "That email is already in use by another account — the pending "
                "change has been canceled."
            )
            return RedirectResponse("/login", status_code=302)

        old_email = user.email
        user.email = pending
        user.pending_email = None
        user.email_verified = True
        await audit.record(
            db,
            action=audit.AUTH_EMAIL_CHANGED,
            request=request,
            actor=user,
            metadata={"old_email": old_email, "new_email": user.email},
        )
        await db.commit()
        request.session["flash_notice"] = (
            f"Email updated to {user.email}."
        )
        return RedirectResponse("/login", status_code=302)

    user.email_verified = True
    await audit.record(
        db, action=audit.AUTH_EMAIL_VERIFIED, request=request, actor=user
    )
    await db.commit()
    request.session["flash_notice"] = "Email verified — you can now log in."
    return RedirectResponse("/login", status_code=302)


@router.get("/auth/resend")
async def resend_page(request: Request, db: AsyncSession = Depends(get_db)):
    smtp = await get_smtp_config(db)
    if smtp is None or not smtp.enabled:
        return RedirectResponse("/login", status_code=302)
    site = await get_site_settings(db)
    error = request.session.pop("flash_error", None)
    notice = request.session.pop("flash_notice", None)
    return templates.TemplateResponse(
        request,
        "auth/resend.html",
        {"site": site, "error": error, "notice": notice},
    )


@router.post("/auth/resend", dependencies=[Depends(rate_limit("resend", 5, 300))])
async def resend_submit(
    request: Request, email: str = Form(...), db: AsyncSession = Depends(get_db)
):
    smtp = await get_smtp_config(db)
    if smtp is None or not smtp.enabled:
        return RedirectResponse("/login", status_code=302)
    user = await user_svc.get_user_by_email(db, email.strip())
    if user is not None and not user.email_verified:
        await invalidate_user_tokens(db, user.id, purpose="verify")
        token = await create_email_token(db, user.id, purpose="verify")
        await db.commit()
        site = await get_site_settings(db)
        verify_url = f"{settings.base_url}/auth/verify/{token.plaintext}"
        try:
            await send_email(
                db,
                to=user.email,
                subject=f"Verify your email for {site.site_name}",
                body=f"Confirm your email here:\n\n{verify_url}\n\nThis link expires in 24 hours. Any previous verification link is no longer valid.",
            )
        except (SMTPException, OSError, RuntimeError):
            logger.warning(
                "Failed to send resend verification email to %s", user.email, exc_info=True
            )
    request.session["flash_notice"] = (
        "If that email matches an unverified account, we've sent a new verification link."
    )
    return RedirectResponse("/auth/resend", status_code=302)


@router.get("/auth/forgot")
async def forgot_page(request: Request, db: AsyncSession = Depends(get_db)):
    smtp = await get_smtp_config(db)
    if smtp is None or not smtp.enabled:
        return RedirectResponse("/login", status_code=302)
    error = request.session.pop("flash_error", None)
    notice = request.session.pop("flash_notice", None)
    return templates.TemplateResponse(
        request, "auth/forgot.html", {"error": error, "notice": notice}
    )


@router.post("/auth/forgot", dependencies=[Depends(rate_limit("forgot", 5, 300))])
async def forgot_submit(
    request: Request, email: str = Form(...), db: AsyncSession = Depends(get_db)
):
    smtp = await get_smtp_config(db)
    if smtp is None or not smtp.enabled:
        return RedirectResponse("/login", status_code=302)
    user = await user_svc.get_user_by_email(db, email.strip())
    if user is not None:
        await invalidate_user_tokens(db, user.id, purpose="reset")
        token = await create_email_token(db, user.id, purpose="reset", ttl_hours=2)
        await audit.record(
            db,
            action=audit.AUTH_PASSWORD_RESET_REQUESTED,
            request=request,
            actor=user,
        )
        await db.commit()
        site = await get_site_settings(db)
        reset_url = f"{settings.base_url}/auth/reset/{token.plaintext}"
        try:
            await send_email(
                db,
                to=user.email,
                subject=f"Reset your password for {site.site_name}",
                body=f"Use this link to reset your password:\n\n{reset_url}\n\nThis link expires in 2 hours. If you didn't request this, ignore this email.",
            )
        except (SMTPException, OSError, RuntimeError):
            logger.warning(
                "Failed to send password-reset email to %s", user.email, exc_info=True
            )
    request.session["flash_notice"] = (
        "If that email matches an account, we've sent a password reset link."
    )
    return RedirectResponse("/auth/forgot", status_code=302)


@router.get("/auth/reset/{token}")
async def reset_page(
    request: Request, token: str, db: AsyncSession = Depends(get_db)
):
    if await find_valid_token(db, token, purpose="reset") is None:
        request.session["flash_error"] = "Reset link is invalid or expired."
        return RedirectResponse("/auth/forgot", status_code=302)
    error = request.session.pop("flash_error", None)
    return templates.TemplateResponse(
        request, "auth/reset.html", {"token": token, "error": error}
    )


@router.post("/auth/reset/{token}", dependencies=[Depends(rate_limit("reset", 10, 300))])
async def reset_submit(
    request: Request,
    token: str,
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if password != password_confirm:
        request.session["flash_error"] = "Passwords do not match."
        return RedirectResponse(f"/auth/reset/{token}", status_code=302)
    if len(password) < 8:
        request.session["flash_error"] = "Password must be at least 8 characters."
        return RedirectResponse(f"/auth/reset/{token}", status_code=302)

    record = await consume_email_token(db, token, purpose="reset")
    if record is None:
        request.session["flash_error"] = "Reset link is invalid or expired."
        return RedirectResponse("/auth/forgot", status_code=302)

    user = await user_svc.get_user_by_id(db, record.user_id)
    if user is None:
        request.session["flash_error"] = "Reset link is invalid."
        return RedirectResponse("/auth/forgot", status_code=302)
    user.password_hash = hash_password(password)
    # Reset invalidates all other sessions for this user.
    await user_svc.bump_session_epoch(db, user.id)
    await audit.record(
        db,
        action=audit.AUTH_PASSWORD_RESET_COMPLETED,
        request=request,
        actor=user,
    )
    await db.commit()
    request.session["flash_notice"] = "Password updated — you can now log in."
    return RedirectResponse("/login", status_code=302)
