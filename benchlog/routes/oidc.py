import uuid
from time import time as _now

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog import audit
from benchlog.config import settings
from benchlog.database import get_db
from benchlog.dependencies import current_user
from benchlog.models import OIDCIdentity, OIDCProvider, User
from benchlog.auth import oidc as oidc_svc
from benchlog.auth import users as user_svc
from benchlog.auth.signup import SignupValidationError, validate_username
from benchlog.rate_limit import rate_limit
from benchlog.site_settings import get_site_settings
from benchlog.templating import templates

# How long the pending OIDC profile-completion state lives in the session.
# Long enough for a user to fill the form, short enough that a stale tab
# left open overnight doesn't auto-create an account hours later.
PENDING_SIGNUP_TTL_SECONDS = 600

router = APIRouter()


def _redirect_uri(slug: str) -> str:
    return f"{settings.base_url.rstrip('/')}/auth/oidc/{slug}/callback"


@router.get("/auth/oidc/{slug}/login")
async def oidc_login(
    request: Request,
    slug: str,
    link: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(current_user),
):
    provider = await oidc_svc.get_provider_by_slug(db, slug)
    if provider is None or not provider.enabled:
        request.session["flash_error"] = "Unknown or disabled login provider."
        return RedirectResponse("/login", status_code=302)

    try:
        metadata = await oidc_svc.fetch_discovery(
            provider.discovery_url, allow_private=provider.allow_private_network
        )
    except oidc_svc.OIDCError as exc:
        request.session["flash_error"] = f"Provider misconfigured: {exc}"
        return RedirectResponse("/login", status_code=302)

    state = oidc_svc.generate_state()
    nonce = oidc_svc.generate_nonce()

    request.session["oidc_flow"] = {
        "provider_slug": slug,
        "state": state,
        "nonce": nonce,
        "link_user_id": str(user.id) if (link and user) else None,
    }

    url = oidc_svc.build_authorize_url(
        metadata=metadata,
        provider=provider,
        redirect_uri=_redirect_uri(slug),
        state=state,
        nonce=nonce,
    )
    return RedirectResponse(url, status_code=302)


@router.get("/auth/oidc/{slug}/callback")
async def oidc_callback(
    request: Request,
    slug: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
    current: User | None = Depends(current_user),
):
    flow = request.session.pop("oidc_flow", None)
    if error:
        request.session["flash_error"] = f"Sign-in failed: {error}"
        return RedirectResponse("/login", status_code=302)
    if not code or not state or flow is None or flow.get("state") != state or flow.get("provider_slug") != slug:
        request.session["flash_error"] = "Invalid OIDC callback."
        return RedirectResponse("/login", status_code=302)

    provider = await oidc_svc.get_provider_by_slug(db, slug)
    if provider is None or not provider.enabled:
        request.session["flash_error"] = "Unknown or disabled login provider."
        return RedirectResponse("/login", status_code=302)

    site = await get_site_settings(db)
    link_uid_str = flow.get("link_user_id")
    link_uid = uuid.UUID(link_uid_str) if link_uid_str else None

    result = await oidc_svc.handle_callback(
        db,
        provider=provider,
        code=code,
        redirect_uri=_redirect_uri(slug),
        nonce=flow["nonce"],
        link_user_id=link_uid,
        current_user_id=current.id if current is not None else None,
        site_requires_email_verification=site.require_email_verification,
    )

    if result.kind == "error":
        request.session["flash_error"] = result.message or "Sign-in failed."
        return RedirectResponse(result.redirect, status_code=302)

    if result.kind == "linked":
        await audit.record(
            db,
            action=audit.AUTH_OIDC_LINKED,
            request=request,
            actor=current,
            target_label=provider.display_name,
            metadata={"provider_slug": slug},
        )
        await db.commit()
        request.session["flash_notice"] = result.message or "Linked."
        return RedirectResponse(result.redirect, status_code=302)

    if result.kind == "needs_profile":
        assert result.profile_data is not None
        request.session["oidc_pending_signup"] = {
            **result.profile_data,
            "provider_slug": slug,
            "expires_at": _now() + PENDING_SIGNUP_TTL_SECONDS,
        }
        return RedirectResponse("/auth/oidc/complete", status_code=302)

    # logged_in — start a fresh session
    assert result.user_id is not None
    user = await user_svc.get_user_by_id(db, result.user_id)
    assert user is not None
    await audit.record(
        db,
        action=audit.AUTH_LOGIN_SUCCESS,
        request=request,
        actor=user,
        metadata={"method": "oidc", "provider_slug": slug},
    )
    await audit.record(
        db,
        action=audit.AUTH_OIDC_LOGIN,
        request=request,
        actor=user,
        target_label=provider.display_name,
        metadata={"provider_slug": slug},
    )
    await db.commit()
    # Rotate session state on login.
    request.session.clear()
    request.session["user"] = {"id": str(user.id), "epoch": user.session_epoch}
    return RedirectResponse("/", status_code=302)


def _pending_signup_valid(pending: dict | None) -> bool:
    if not isinstance(pending, dict):
        return False
    expires_at = pending.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        return False
    return _now() <= expires_at


@router.get("/auth/oidc/complete")
async def oidc_complete_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(current_user),
):
    if user is not None:
        return RedirectResponse("/", status_code=302)

    pending = request.session.get("oidc_pending_signup")
    if not _pending_signup_valid(pending):
        request.session.pop("oidc_pending_signup", None)
        request.session["flash_error"] = "Sign-in session expired — please try again."
        return RedirectResponse("/login", status_code=302)

    site = await get_site_settings(db)
    error = request.session.pop("flash_error", None)
    # Prefer the user's last submitted values (from a validation bounce) over
    # the provider-derived prefills so edits survive a failed submit.
    username = request.session.pop(
        "oidc_signup_username_draft", pending["username_prefill"]
    )
    display_name = request.session.pop(
        "oidc_signup_display_name_draft", pending["display_name_prefill"]
    )
    return templates.TemplateResponse(
        request,
        "auth/oidc_complete.html",
        {
            "site": site,
            "error": error,
            "email": pending["email"],
            "username": username,
            "display_name": display_name,
            "provider_slug": pending["provider_slug"],
        },
    )


@router.post(
    "/auth/oidc/complete",
    dependencies=[Depends(rate_limit("oidc_complete", 10, 300))],
)
async def oidc_complete_submit(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    pending = request.session.get("oidc_pending_signup")
    if not _pending_signup_valid(pending):
        request.session.pop("oidc_pending_signup", None)
        request.session["flash_error"] = "Sign-in session expired — please try again."
        return RedirectResponse("/login", status_code=302)

    def _bounce(error: str):
        request.session["flash_error"] = error
        request.session["oidc_signup_username_draft"] = username
        request.session["oidc_signup_display_name_draft"] = display_name
        return RedirectResponse("/auth/oidc/complete", status_code=302)

    try:
        username_clean = validate_username(username)
    except SignupValidationError as exc:
        return _bounce(str(exc))

    if await user_svc.get_user_by_username(db, username_clean):
        return _bounce("That username is taken.")

    # Re-fetch the provider so a disabled/deleted provider can't still
    # mint an account from a stale pending-signup session.
    provider = await db.get(OIDCProvider, uuid.UUID(pending["provider_id"]))
    if provider is None or not provider.enabled or not provider.auto_create_users:
        request.session.pop("oidc_pending_signup", None)
        request.session["flash_error"] = "That sign-in provider is unavailable."
        return RedirectResponse("/login", status_code=302)

    # Re-check email isn't claimed mid-flow (another signup may have raced us).
    if await user_svc.get_user_by_email_or_pending(db, pending["email"]):
        request.session.pop("oidc_pending_signup", None)
        request.session["flash_error"] = (
            "An account with that email already exists — please sign in."
        )
        return RedirectResponse("/login", status_code=302)

    first_run = await user_svc.user_count(db) == 0
    new_user = User(
        email=pending["email"],
        username=username_clean,
        display_name=display_name.strip() or username_clean,
        password_hash=None,
        email_verified=bool(pending["email_verified"]),
        is_site_admin=first_run,
        is_active=True,
    )
    db.add(new_user)
    await db.flush()
    db.add(
        OIDCIdentity(
            user_id=new_user.id,
            provider_id=provider.id,
            subject=pending["subject"],
            email=pending["email"],
        )
    )
    await audit.record(
        db,
        action=audit.AUTH_SIGNUP,
        request=request,
        actor=new_user,
        metadata={"method": "oidc", "provider_slug": provider.slug},
    )
    await audit.record(
        db,
        action=audit.AUTH_LOGIN_SUCCESS,
        request=request,
        actor=new_user,
        metadata={"method": "oidc", "provider_slug": provider.slug},
    )
    await audit.record(
        db,
        action=audit.AUTH_OIDC_LOGIN,
        request=request,
        actor=new_user,
        target_label=provider.display_name,
        metadata={"provider_slug": provider.slug},
    )
    await db.commit()
    request.session.clear()
    request.session["user"] = {"id": str(new_user.id), "epoch": new_user.session_epoch}
    return RedirectResponse("/", status_code=302)
