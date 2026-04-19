import uuid

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog import audit
from benchlog.database import get_db
from benchlog.dependencies import require_admin
from benchlog.models import OIDCProvider, User
from benchlog.auth.oidc import OIDCError, fetch_discovery
from benchlog.templating import templates

router = APIRouter(prefix="/oidc")


def _clean_slug(value: str) -> str:
    return "".join(ch for ch in value.strip().lower() if ch.isalnum() or ch in "-_")


@router.get("")
async def list_providers(
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(OIDCProvider).order_by(OIDCProvider.display_name))
    providers = list(result.scalars().all())
    error = request.session.pop("flash_error", None)
    notice = request.session.pop("flash_notice", None)
    return templates.TemplateResponse(
        request,
        "admin/oidc_list.html",
        {"user": admin, "providers": providers, "error": error, "notice": notice},
    )


@router.get("/new")
async def new_provider(
    request: Request,
    admin: User = Depends(require_admin),
):
    error = request.session.pop("flash_error", None)
    return templates.TemplateResponse(
        request,
        "admin/oidc_edit.html",
        {"user": admin, "provider": None, "error": error},
    )


@router.get("/{provider_id}")
async def edit_provider(
    request: Request,
    provider_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    provider = await db.get(OIDCProvider, provider_id)
    if provider is None:
        raise HTTPException(404)
    error = request.session.pop("flash_error", None)
    notice = request.session.pop("flash_notice", None)
    return templates.TemplateResponse(
        request,
        "admin/oidc_edit.html",
        {"user": admin, "provider": provider, "error": error, "notice": notice},
    )


@router.post("/save")
async def save_provider(
    request: Request,
    id: str = Form(""),
    slug: str = Form(...),
    display_name: str = Form(...),
    discovery_url: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(""),
    scopes: str = Form("openid email profile"),
    enabled: bool = Form(False),
    auto_create_users: bool = Form(False),
    auto_link_verified_email: bool = Form(False),
    allow_private_network: bool = Form(False),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    slug_clean = _clean_slug(slug)
    if len(slug_clean) < 2:
        request.session["flash_error"] = "Slug must be at least 2 characters (letters/numbers/-/_)."
        return RedirectResponse("/admin/oidc/new" if not id else f"/admin/oidc/{id}", status_code=302)

    if id:
        provider = await db.get(OIDCProvider, uuid.UUID(id))
        if provider is None:
            raise HTTPException(404)
    else:
        existing = await db.execute(select(OIDCProvider).where(OIDCProvider.slug == slug_clean))
        if existing.scalar_one_or_none():
            request.session["flash_error"] = "A provider with that slug already exists."
            return RedirectResponse("/admin/oidc/new", status_code=302)
        provider = OIDCProvider(slug=slug_clean)
        db.add(provider)

    provider.slug = slug_clean
    provider.display_name = display_name.strip()
    provider.discovery_url = discovery_url.strip()
    provider.client_id = client_id.strip()
    if client_secret.strip():
        provider.client_secret = client_secret.strip()
    provider.scopes = scopes.strip() or "openid email profile"
    provider.enabled = enabled
    provider.auto_create_users = auto_create_users
    provider.auto_link_verified_email = auto_link_verified_email
    provider.allow_private_network = allow_private_network
    await db.flush()
    await audit.record(
        db,
        action=audit.ADMIN_OIDC_PROVIDER_SAVED,
        request=request,
        actor=admin,
        target_type="oidc_provider",
        target_id=provider.id,
        target_label=provider.display_name,
        metadata={"slug": provider.slug, "enabled": provider.enabled},
    )
    await db.commit()
    request.session["flash_notice"] = f"Saved {provider.display_name}."
    return RedirectResponse("/admin/oidc", status_code=302)


@router.post("/{provider_id}/test")
async def test_provider(
    request: Request,
    provider_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    provider = await db.get(OIDCProvider, provider_id)
    if provider is None:
        raise HTTPException(404)
    try:
        metadata = await fetch_discovery(
            provider.discovery_url, allow_private=provider.allow_private_network
        )
        issuer = metadata.get("issuer", "?")
        request.session["flash_notice"] = f"Discovery OK — issuer: {issuer}"
    except (OIDCError, httpx.HTTPError) as exc:
        request.session["flash_error"] = f"Discovery failed: {exc}"
    return RedirectResponse(f"/admin/oidc/{provider_id}", status_code=302)


@router.post("/{provider_id}/delete")
async def delete_provider(
    request: Request,
    provider_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    provider = await db.get(OIDCProvider, provider_id)
    if provider is None:
        raise HTTPException(404)
    await audit.record(
        db,
        action=audit.ADMIN_OIDC_PROVIDER_DELETED,
        request=request,
        actor=admin,
        target_type="oidc_provider",
        target_id=provider.id,
        target_label=provider.display_name,
        metadata={"slug": provider.slug},
    )
    await db.delete(provider)
    await db.commit()
    request.session["flash_notice"] = "Provider deleted."
    return RedirectResponse("/admin/oidc", status_code=302)
