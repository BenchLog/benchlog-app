import ipaddress
import logging
import secrets
import socket
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.config import settings
from benchlog.models import OIDCIdentity, OIDCProvider, User

logger = logging.getLogger("benchlog.oidc")

_ALLOWED_ID_TOKEN_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256"]

_LOCAL_BASE = settings.base_url.startswith(("http://localhost", "http://127.0.0.1"))


class OIDCError(Exception):
    pass


def _is_localhost_host(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1")


def _guard_url(url: str, *, allow_private: bool = False) -> None:
    """Reject URLs that could enable SSRF against internal services.

    - Must be http(s). HTTPS required unless base_url is localhost (dev).
    - Hostname must resolve to a public IP — block loopback/private/link-local.

    Pass `allow_private=True` to permit private/loopback/link-local addresses.
    Providers set this via OIDCProvider.allow_private_network for LAN-hosted
    IdPs (e.g. a homelab Authelia). HTTPS is still enforced off-localhost.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise OIDCError(f"URL scheme must be http(s): {parsed.scheme}")
    if parsed.scheme == "http" and not _LOCAL_BASE and not allow_private:
        raise OIDCError("OIDC URLs must use HTTPS outside localhost dev")
    host = parsed.hostname
    if not host:
        raise OIDCError("URL has no host")
    # Localhost is allowed in dev only, or when the provider opts in.
    if _is_localhost_host(host):
        if _LOCAL_BASE or allow_private:
            return
        raise OIDCError("OIDC URL points to localhost")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise OIDCError(f"Could not resolve {host}: {exc}") from exc
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise OIDCError(f"OIDC URL resolves to non-public address: {ip}")


async def _safe_get(url: str, *, allow_private: bool = False, **kwargs) -> httpx.Response:
    _guard_url(url, allow_private=allow_private)
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False, **kwargs) as client:
        return await client.get(url)


async def get_enabled_providers(db: AsyncSession) -> list[OIDCProvider]:
    result = await db.execute(
        select(OIDCProvider).where(OIDCProvider.enabled.is_(True)).order_by(OIDCProvider.display_name)
    )
    return list(result.scalars().all())


async def get_provider_by_slug(db: AsyncSession, slug: str) -> OIDCProvider | None:
    result = await db.execute(select(OIDCProvider).where(OIDCProvider.slug == slug))
    return result.scalar_one_or_none()


async def fetch_discovery(discovery_url: str, *, allow_private: bool = False) -> dict:
    """Fetch OIDC discovery document. Validates issuer origin matches discovery origin."""
    response = await _safe_get(discovery_url, allow_private=allow_private)
    response.raise_for_status()
    metadata = response.json()
    issuer = metadata.get("issuer")
    if not issuer:
        raise OIDCError("Discovery document missing 'issuer'")
    # Issuer origin must match discovery URL origin — prevents a hostile
    # discovery response from impersonating a different identity provider.
    disc_parsed = urlparse(discovery_url)
    iss_parsed = urlparse(issuer)
    if (disc_parsed.scheme, disc_parsed.netloc) != (iss_parsed.scheme, iss_parsed.netloc):
        raise OIDCError(
            f"Issuer origin {iss_parsed.scheme}://{iss_parsed.netloc} "
            f"does not match discovery URL origin"
        )
    return metadata


def build_authorize_url(
    metadata: dict, provider: OIDCProvider, redirect_uri: str, state: str, nonce: str
) -> str:
    params = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "scope": provider.scopes,
        "state": state,
        "nonce": nonce,
    }
    return f"{metadata['authorization_endpoint']}?{urlencode(params)}"


async def exchange_code(
    metadata: dict, provider: OIDCProvider, code: str, redirect_uri: str
) -> dict:
    token_endpoint = metadata["token_endpoint"]
    _guard_url(token_endpoint, allow_private=provider.allow_private_network)
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        response = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": provider.client_id,
                "client_secret": provider.client_secret,
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            # Don't log response.text — some IdPs echo submitted params
            # (including client_secret in WWW-Authenticate-style errors).
            logger.warning(
                "OIDC token exchange failed: status=%s www-authenticate=%r",
                response.status_code,
                response.headers.get("www-authenticate"),
            )
            raise OIDCError(f"token exchange failed: {response.status_code}")
        return response.json()


async def verify_id_token(metadata: dict, id_token: str, provider: OIDCProvider, nonce: str) -> dict:
    jwks_response = await _safe_get(
        metadata["jwks_uri"], allow_private=provider.allow_private_network
    )
    jwks_response.raise_for_status()
    jwks = jwks_response.json()

    key_set = KeySet.import_key_set(jwks)
    token = jwt.decode(id_token, key_set, algorithms=_ALLOWED_ID_TOKEN_ALGS)
    # exp/iat are essential — joserfc only validates claims we register.
    registry = JWTClaimsRegistry(
        iss={"essential": True, "value": metadata["issuer"]},
        aud={"essential": True, "value": provider.client_id},
        nonce={"essential": True, "value": nonce},
        exp={"essential": True},
        iat={"essential": True},
        nbf={"essential": False},
    )
    registry.validate(token.claims)
    return dict(token.claims)


async def fetch_userinfo(
    metadata: dict, access_token: str, *, allow_private: bool = False
) -> dict:
    if "userinfo_endpoint" not in metadata:
        return {}
    response = await _safe_get(
        metadata["userinfo_endpoint"],
        allow_private=allow_private,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    response.raise_for_status()
    return response.json()


def generate_state() -> str:
    return secrets.token_urlsafe(32)


def generate_nonce() -> str:
    return secrets.token_urlsafe(32)


# ---------- callback orchestration ----------


@dataclass
class CallbackResult:
    """Result of OIDC callback. Exactly one of user_id / error_message is set.

    `needs_profile` is returned in place of auto-creating a user when a new
    OIDC identity arrives. Usernames are immutable after signup, so we show
    the user a completion form rather than silently auto-assigning a slug
    derived from their provider profile. `profile_data` carries the prefill
    values and the verified provider claims the completion route needs.
    """
    kind: str  # "linked" | "logged_in" | "needs_profile" | "error"
    user_id: uuid.UUID | None = None
    message: str | None = None
    redirect: str = "/"
    profile_data: dict | None = None


def _clean_username_from_email(email: str) -> str:
    """Derive a slug-safe username prefill from an email local-part.

    Must produce output that passes benchlog.auth.signup.validate_username:
    lowercase [a-z0-9_-], 2-32 chars, starts and ends with alphanumeric.
    Shorter results are padded; empty input falls back to "user". Only a
    prefill — the user confirms or edits it on the completion page, so
    collisions and reserved names aren't checked here.
    """
    base = email.split("@", 1)[0].lower()
    cleaned = "".join(ch for ch in base if ch.isalnum() or ch in "-_")
    cleaned = cleaned.strip("-_")[:32]
    if len(cleaned) < 2:
        cleaned = (cleaned + "user")[:32]
    return cleaned


async def handle_callback(
    db: AsyncSession,
    *,
    provider: OIDCProvider,
    code: str,
    redirect_uri: str,
    nonce: str,
    link_user_id: uuid.UUID | None,
    current_user_id: uuid.UUID | None,
    site_requires_email_verification: bool,
) -> CallbackResult:
    """Exchange code, verify ID token, and dispatch to link/login/create.

    link_user_id is the user who initiated an /auth/oidc/{slug}/login?link=true
    flow; current_user_id is the session's present user. They must match before
    we attach a new identity — otherwise a session-swap mid-flow could link to
    the wrong user.
    """
    from benchlog.auth import users as user_svc

    metadata = await fetch_discovery(
        provider.discovery_url, allow_private=provider.allow_private_network
    )
    try:
        token_response = await exchange_code(metadata, provider, code, redirect_uri)
    except OIDCError:
        return CallbackResult(kind="error", message="Could not complete sign-in with that provider.", redirect="/login")

    id_token = token_response.get("id_token")
    if not id_token:
        return CallbackResult(kind="error", message="Provider did not return an ID token.", redirect="/login")

    try:
        claims = await verify_id_token(metadata, id_token, provider, nonce)
    except (JoseError, OIDCError, httpx.HTTPError, KeyError):
        logger.warning("OIDC id_token verification failed", exc_info=True)
        return CallbackResult(kind="error", message="Could not verify ID token from provider.", redirect="/login")

    subject = claims.get("sub")
    if not subject:
        return CallbackResult(kind="error", message="Provider did not return a subject claim.", redirect="/login")

    # Some providers (e.g. Zitadel) don't include userinfo claims in the id_token.
    if not claims.get("email") and token_response.get("access_token"):
        try:
            userinfo = await fetch_userinfo(
                metadata,
                token_response["access_token"],
                allow_private=provider.allow_private_network,
            )
        except (OIDCError, httpx.HTTPError):
            userinfo = {}
        if userinfo.get("sub") and userinfo["sub"] != subject:
            return CallbackResult(kind="error", message="Userinfo response did not match ID token.", redirect="/login")
        for key in ("email", "email_verified", "name", "preferred_username"):
            if key not in claims and key in userinfo:
                claims[key] = userinfo[key]

    email = (claims.get("email") or "").strip().lower()
    email_verified_claim = bool(claims.get("email_verified"))
    display_name = claims.get("name") or claims.get("preferred_username") or email or "User"
    preferred_username = claims.get("preferred_username")

    # Link branch: logged-in user explicitly connecting a new provider.
    if link_user_id is not None:
        if current_user_id != link_user_id:
            # Session changed mid-flow (logout, different user). Refuse to link.
            return CallbackResult(kind="error", message="Link flow interrupted — please sign in and try again.", redirect="/login")
        existing = (
            await db.execute(
                select(OIDCIdentity).where(
                    OIDCIdentity.provider_id == provider.id,
                    OIDCIdentity.subject == subject,
                )
            )
        ).scalar_one_or_none()
        if existing is not None and existing.user_id != link_user_id:
            return CallbackResult(kind="error", message="That provider account is already linked to a different user.", redirect="/account")
        if existing is None:
            db.add(
                OIDCIdentity(
                    user_id=link_user_id,
                    provider_id=provider.id,
                    subject=subject,
                    email=email or None,
                )
            )
            await db.commit()
        return CallbackResult(
            kind="linked",
            user_id=link_user_id,
            message=f"Linked {provider.display_name}.",
            redirect="/account",
        )

    # Login branch — existing identity for this (provider, subject).
    identity = (
        await db.execute(
            select(OIDCIdentity).where(
                OIDCIdentity.provider_id == provider.id, OIDCIdentity.subject == subject
            )
        )
    ).scalar_one_or_none()

    if identity is not None:
        user = await user_svc.get_user_by_id(db, identity.user_id)
        if user is None or not user.is_active:
            return CallbackResult(kind="error", message="That account is disabled.", redirect="/login")
        if email and identity.email != email:
            identity.email = email
        await db.commit()
        return CallbackResult(kind="logged_in", user_id=user.id)

    # Auto-link by verified email (if provider opts in and both sides assert verified).
    if email:
        existing_user = await user_svc.get_user_by_email(db, email)
        if existing_user is not None:
            can_auto_link = (
                provider.auto_link_verified_email
                and email_verified_claim
                and existing_user.email_verified
            )
            if can_auto_link:
                db.add(
                    OIDCIdentity(
                        user_id=existing_user.id,
                        provider_id=provider.id,
                        subject=subject,
                        email=email,
                    )
                )
                await db.commit()
                return CallbackResult(kind="logged_in", user_id=existing_user.id)
            return CallbackResult(
                kind="error",
                message=(
                    f"An account with {email} already exists. Log in with your existing method, "
                    f"then link {provider.display_name} from Account Settings."
                ),
                redirect="/login",
            )

    # Auto-create flow.
    if not provider.auto_create_users:
        return CallbackResult(
            kind="error",
            message=f"{provider.display_name} can't create new accounts. Ask an administrator to create one for you.",
            redirect="/login",
        )

    if not email:
        return CallbackResult(kind="error", message="Provider did not return an email — cannot create account.", redirect="/login")

    if site_requires_email_verification and not email_verified_claim:
        return CallbackResult(
            kind="error",
            message=(
                f"Your {provider.display_name} account's email isn't verified, and this site "
                f"requires email verification. Verify your email with {provider.display_name} "
                f"first, or contact an administrator."
            ),
            redirect="/login",
        )

    # Hand off to the completion page so the user picks their own username.
    # Usernames are immutable, so we must not silently auto-assign a slug
    # derived from the provider's preferred_username or email local-part.
    return CallbackResult(
        kind="needs_profile",
        profile_data={
            "provider_id": str(provider.id),
            "subject": subject,
            "email": email,
            "email_verified": email_verified_claim,
            "display_name_prefill": display_name,
            "username_prefill": _clean_username_from_email(preferred_username or email),
        },
    )
