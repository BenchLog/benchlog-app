"""Passkey (WebAuthn) flows.

- /auth/passkey/start, /auth/passkey/finish — login with discoverable credential
- /signup/passkey/start, /signup/passkey/finish — sign up without a password
- /account/passkeys/register/start, /finish — add a passkey to the current user
- /account/passkeys/{id}/rename, /delete — manage stored passkeys
"""

import base64
import json
import logging
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn.helpers import options_to_json
from webauthn.helpers.exceptions import WebAuthnException

from benchlog import audit
from benchlog.database import get_db
from benchlog.dependencies import current_user, require_user
from benchlog.models import User, WebAuthnCredential
from benchlog.auth import users as user_svc
from benchlog.auth import webauthn as wa
from benchlog.auth.signup import (
    EmailAlreadyRegistered,
    SignupValidationError,
    validate_signup_fields,
)
from benchlog.auth.users import get_user_by_id
from benchlog.rate_limit import rate_limit
from benchlog.site_settings import get_site_settings

logger = logging.getLogger("benchlog.passkeys")

router = APIRouter()


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _login_session(request: Request, user: User) -> None:
    """Start a fresh authenticated session — clears stale state first."""
    request.session.clear()
    request.session["user"] = {"id": str(user.id), "epoch": user.session_epoch}


# ---------- registration (logged-in user adds a passkey) ----------


@router.post("/account/passkeys/register/start")
async def register_start(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    creds = (
        await db.execute(
            select(WebAuthnCredential).where(WebAuthnCredential.user_id == user.id)
        )
    ).scalars().all()

    options = wa.make_registration_options(user, list(creds))
    request.session["webauthn_register_challenge"] = base64.urlsafe_b64encode(
        options.challenge
    ).decode()
    return JSONResponse(json.loads(options_to_json(options)))


@router.post("/account/passkeys/register/finish")
async def register_finish(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    challenge_b64 = request.session.pop("webauthn_register_challenge", None)
    if not challenge_b64:
        raise HTTPException(400, "No registration in progress")
    expected_challenge = _b64url_decode(challenge_b64)

    body = await request.json()
    friendly_name = (body.get("friendly_name") or "Passkey").strip()[:128] or "Passkey"

    try:
        verified = wa.verify_registration(credential=body, expected_challenge=expected_challenge)
    except WebAuthnException as exc:
        logger.warning("passkey registration verification failed", exc_info=True)
        raise HTTPException(400, "Verification failed") from exc

    transports = ",".join(t for t in (body.get("response", {}).get("transports") or []))
    cred = WebAuthnCredential(
        user_id=user.id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=transports[:128],
        friendly_name=friendly_name,
    )
    db.add(cred)
    await db.flush()
    await audit.record(
        db,
        action=audit.AUTH_PASSKEY_REGISTERED,
        request=request,
        actor=user,
        target_type="passkey",
        target_id=cred.id,
        target_label=friendly_name,
    )
    await db.commit()
    return JSONResponse({"ok": True})


# ---------- management ----------


@router.post("/account/passkeys/{credential_id}/rename")
async def rename_passkey(
    request: Request,
    credential_id: uuid.UUID,
    friendly_name: str = Form(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    cred = (
        await db.execute(
            select(WebAuthnCredential).where(
                WebAuthnCredential.id == credential_id,
                WebAuthnCredential.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if cred is None:
        request.session["flash_error"] = "Passkey not found."
        return RedirectResponse("/account", status_code=302)
    cred.friendly_name = (friendly_name.strip() or "Passkey")[:128]
    await db.commit()
    return RedirectResponse("/account", status_code=302)


@router.post("/account/passkeys/{credential_id}/delete")
async def delete_passkey(
    request: Request,
    credential_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    cred = (
        await db.execute(
            select(WebAuthnCredential).where(
                WebAuthnCredential.id == credential_id,
                WebAuthnCredential.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if cred is None:
        request.session["flash_error"] = "Passkey not found."
        return RedirectResponse("/account", status_code=302)

    if not await user_svc.other_sign_in_methods_exist(
        db, user, excluding_passkey_id=credential_id
    ):
        request.session["flash_error"] = (
            "Can't remove your only sign-in method. Set a password or link a provider first."
        )
        return RedirectResponse("/account", status_code=302)

    cred_label = cred.friendly_name
    cred_id = cred.id
    await audit.record(
        db,
        action=audit.AUTH_PASSKEY_REMOVED,
        request=request,
        actor=user,
        target_type="passkey",
        target_id=cred_id,
        target_label=cred_label,
    )
    await db.delete(cred)
    await db.commit()
    request.session["flash_notice"] = "Passkey removed."
    return RedirectResponse("/account", status_code=302)


# ---------- signup with passkey (no password) ----------


@router.post("/signup/passkey/start", dependencies=[Depends(rate_limit("passkey_signup", 5, 300))])
async def signup_passkey_start(
    request: Request,
    email: str = Form(...),
    username: str = Form(...),
    display_name: str = Form(...),
    user: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if user is not None:
        raise HTTPException(400, "Already signed in")

    site = await get_site_settings(db)
    total_users = await user_svc.user_count(db)
    first_run = total_users == 0
    if not first_run and not site.allow_local_signup:
        raise HTTPException(403, "Local signup is disabled.")

    email_collision = False
    try:
        fields = await validate_signup_fields(db, email, username, display_name)
    except EmailAlreadyRegistered:
        # Return indistinguishable response — finish handler will skip user creation.
        email_collision = True
        fields = None
    except SignupValidationError as exc:
        raise HTTPException(400, str(exc))

    pending_user_id = uuid.uuid4()
    chosen_email = email.strip() if email_collision else fields.email
    chosen_username = username.strip() if email_collision else fields.username
    chosen_display = display_name.strip() if email_collision else fields.display_name

    options = wa.make_signup_registration_options(
        user_id_bytes=pending_user_id.bytes,
        email=chosen_email,
        display_name=chosen_display,
    )
    request.session["pending_signup"] = {
        "user_id": str(pending_user_id),
        "email": chosen_email,
        "username": chosen_username,
        "display_name": chosen_display,
        "challenge": base64.urlsafe_b64encode(options.challenge).decode(),
        "first_run": first_run,
        "email_collision": email_collision,
    }
    return JSONResponse(json.loads(options_to_json(options)))


@router.post("/signup/passkey/finish")
async def signup_passkey_finish(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    pending = request.session.pop("pending_signup", None)
    if not pending:
        raise HTTPException(400, "No signup in progress")

    body = await request.json()
    expected_challenge = _b64url_decode(pending["challenge"])

    if pending.get("email_collision"):
        # Indistinguishable response: go through the WebAuthn ceremony motions
        # but persist nothing. Matches password-signup's silent-collision behavior.
        try:
            wa.verify_registration(credential=body, expected_challenge=expected_challenge)
        except WebAuthnException:
            pass
        return JSONResponse({"ok": True, "redirect": "/login"})

    try:
        await validate_signup_fields(
            db, pending["email"], pending["username"], pending["display_name"]
        )
    except EmailAlreadyRegistered:
        return JSONResponse({"ok": True, "redirect": "/login"})
    except SignupValidationError as exc:
        raise HTTPException(400, str(exc))

    friendly_name = (body.get("friendly_name") or "Passkey").strip()[:128] or "Passkey"

    try:
        verified = wa.verify_registration(credential=body, expected_challenge=expected_challenge)
    except WebAuthnException as exc:
        logger.warning("passkey signup verification failed", exc_info=True)
        raise HTTPException(400, "Verification failed") from exc

    user = User(
        id=uuid.UUID(pending["user_id"]),
        email=pending["email"],
        username=pending["username"],
        display_name=pending["display_name"],
        password_hash=None,
        is_site_admin=pending["first_run"],
        email_verified=pending["first_run"],
        is_active=True,
    )
    db.add(user)
    await db.flush()
    transports = ",".join(t for t in (body.get("response", {}).get("transports") or []))
    cred = WebAuthnCredential(
        user_id=user.id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=transports[:128],
        friendly_name=friendly_name,
    )
    db.add(cred)
    await db.flush()
    await audit.record(
        db,
        action=audit.AUTH_SIGNUP,
        request=request,
        actor=user,
        metadata={"method": "passkey", "first_run": pending["first_run"]},
    )
    await audit.record(
        db,
        action=audit.AUTH_PASSKEY_REGISTERED,
        request=request,
        actor=user,
        target_type="passkey",
        target_id=cred.id,
        target_label=friendly_name,
    )
    await db.commit()

    _login_session(request, user)
    return JSONResponse({"ok": True, "redirect": "/"})


# ---------- authentication (login with passkey) ----------


@router.post("/auth/passkey/start", dependencies=[Depends(rate_limit("passkey_auth", 10, 60))])
async def auth_start(
    request: Request,
    user: User | None = Depends(current_user),
):
    if user is not None:
        return RedirectResponse("/", status_code=302)
    options = wa.make_authentication_options()
    request.session["webauthn_auth_challenge"] = base64.urlsafe_b64encode(
        options.challenge
    ).decode()
    return JSONResponse(json.loads(options_to_json(options)))


@router.post("/auth/passkey/finish", dependencies=[Depends(rate_limit("passkey_auth", 10, 60))])
async def auth_finish(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    challenge_b64 = request.session.pop("webauthn_auth_challenge", None)
    if not challenge_b64:
        raise HTTPException(400, "No authentication in progress")
    expected_challenge = _b64url_decode(challenge_b64)

    body = await request.json()
    raw_id = body.get("rawId") or body.get("id")
    if not raw_id:
        raise HTTPException(400, "Verification failed")
    credential_id_bytes = _b64url_decode(raw_id)

    stored = (
        await db.execute(
            select(WebAuthnCredential).where(
                WebAuthnCredential.credential_id == credential_id_bytes
            )
        )
    ).scalar_one_or_none()
    if stored is None:
        raise HTTPException(400, "Verification failed")

    user = await get_user_by_id(db, stored.user_id)
    if user is None or not user.is_active:
        raise HTTPException(400, "Verification failed")

    try:
        verified = wa.verify_authentication(
            credential=body, expected_challenge=expected_challenge, stored=stored
        )
    except WebAuthnException as exc:
        logger.warning("passkey auth verification failed", exc_info=True)
        raise HTTPException(400, "Verification failed") from exc

    # WebAuthn §6.1.3: a non-increasing sign count (when the authenticator uses
    # one) suggests the credential was cloned. Authenticators that don't support
    # counters report 0 forever — leave those alone.
    if verified.new_sign_count != 0 and verified.new_sign_count <= stored.sign_count:
        logger.warning(
            "passkey sign_count did not advance for credential %s (stored=%d new=%d) — possible clone",
            stored.id,
            stored.sign_count,
            verified.new_sign_count,
        )
        await audit.record(
            db,
            action=audit.AUTH_PASSKEY_CLONE_DETECTED,
            request=request,
            actor=user,
            outcome="failure",
            target_type="passkey",
            target_id=stored.id,
            target_label=stored.friendly_name,
            metadata={
                "stored_sign_count": stored.sign_count,
                "new_sign_count": verified.new_sign_count,
            },
        )
        await db.commit()
        raise HTTPException(400, "Verification failed")

    stored.sign_count = verified.new_sign_count
    await audit.record(
        db,
        action=audit.AUTH_LOGIN_SUCCESS,
        request=request,
        actor=user,
        metadata={"method": "passkey"},
    )
    await audit.record(
        db,
        action=audit.AUTH_PASSKEY_LOGIN,
        request=request,
        actor=user,
        target_type="passkey",
        target_id=stored.id,
        target_label=stored.friendly_name,
    )
    await db.commit()

    _login_session(request, user)
    return JSONResponse({"ok": True, "redirect": "/"})
