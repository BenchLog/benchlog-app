import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog import audit
from benchlog.config import settings
from benchlog.database import get_db
from benchlog.dependencies import current_user
from benchlog.models import User
from benchlog.auth import oidc as oidc_svc
from benchlog.auth import users as user_svc
from benchlog.site_settings import get_site_settings

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

    # logged_in / created — start a fresh session
    assert result.user_id is not None
    user = await user_svc.get_user_by_id(db, result.user_id)
    assert user is not None
    if result.kind == "created":
        await audit.record(
            db,
            action=audit.AUTH_SIGNUP,
            request=request,
            actor=user,
            metadata={"method": "oidc", "provider_slug": slug},
        )
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
