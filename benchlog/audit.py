"""Append-only audit log service.

Single entry point for recording security- and admin-relevant events.
Designed as a general-purpose activity log: auth, admin actions, and future
feature events all funnel through here.

Action naming convention: `<domain>.<entity>.<verb>` (lowercase, dotted).
Group constants by domain below to keep the namespace discoverable.

Recording is fire-and-forget from the caller's perspective: failures are
logged but never raised — an audit-table outage must not break a login.
"""

import logging
import uuid
from typing import Any

from fastapi import Request
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.models import AuditEvent, User

logger = logging.getLogger("benchlog.audit")

# ---- action namespace ----------------------------------------------------
# Auth domain
AUTH_LOGIN_SUCCESS = "auth.login.success"
AUTH_LOGIN_FAILED = "auth.login.failed"
AUTH_LOGIN_BLOCKED_DISABLED = "auth.login.blocked_disabled"
AUTH_LOGIN_BLOCKED_UNVERIFIED = "auth.login.blocked_unverified"
AUTH_LOGOUT = "auth.logout"
AUTH_SIGNUP = "auth.signup"
AUTH_PASSWORD_CHANGED = "auth.password.changed"
AUTH_PASSWORD_REMOVED = "auth.password.removed"
AUTH_PASSWORD_RESET_REQUESTED = "auth.password.reset_requested"
AUTH_PASSWORD_RESET_COMPLETED = "auth.password.reset_completed"
AUTH_EMAIL_VERIFIED = "auth.email.verified"
AUTH_EMAIL_CHANGE_REQUESTED = "auth.email.change_requested"
AUTH_EMAIL_CHANGE_CANCELED = "auth.email.change_canceled"
AUTH_EMAIL_CHANGED = "auth.email.changed"
AUTH_OIDC_LOGIN = "auth.oidc.login"
AUTH_OIDC_LINKED = "auth.oidc.linked"
AUTH_OIDC_UNLINKED = "auth.oidc.unlinked"
AUTH_PASSKEY_REGISTERED = "auth.passkey.registered"
AUTH_PASSKEY_REMOVED = "auth.passkey.removed"
AUTH_PASSKEY_LOGIN = "auth.passkey.login"
AUTH_PASSKEY_CLONE_DETECTED = "auth.passkey.clone_detected"

# Account domain (self-service)
ACCOUNT_PROFILE_UPDATED = "account.profile.updated"
ACCOUNT_DELETED = "account.deleted"

# Admin domain
ADMIN_USER_DISABLED = "admin.user.disabled"
ADMIN_USER_ENABLED = "admin.user.enabled"
ADMIN_USER_PROMOTED = "admin.user.promoted"
ADMIN_USER_DEMOTED = "admin.user.demoted"
ADMIN_USER_PASSWORD_RESET = "admin.user.password_reset"
ADMIN_USER_DELETED = "admin.user.deleted"
ADMIN_SETTINGS_UPDATED = "admin.settings.updated"
ADMIN_SMTP_UPDATED = "admin.smtp.updated"
ADMIN_OIDC_PROVIDER_SAVED = "admin.oidc_provider.saved"
ADMIN_OIDC_PROVIDER_DELETED = "admin.oidc_provider.deleted"

# Files domain — GPS quarantine flow
FILES_UPLOAD_GPS_DETECTED = "files.upload.gps_detected"
FILES_UPLOAD_GPS_CLEAN = "files.upload.gps_clean"
FILES_GPS_STRIPPED = "files.gps.stripped"
FILES_GPS_RELEASED = "files.gps.released"
FILES_GPS_DISCARDED = "files.gps.discarded"


# ---- action catalog ------------------------------------------------------
# Grouped for the audit-page filter UI. Keeping this next to the constants
# means a new action shows up in the filter as soon as it's defined here.
ACTIONS_BY_DOMAIN: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Auth",
        (
            AUTH_LOGIN_SUCCESS,
            AUTH_LOGIN_FAILED,
            AUTH_LOGIN_BLOCKED_DISABLED,
            AUTH_LOGIN_BLOCKED_UNVERIFIED,
            AUTH_LOGOUT,
            AUTH_SIGNUP,
            AUTH_PASSWORD_CHANGED,
            AUTH_PASSWORD_REMOVED,
            AUTH_PASSWORD_RESET_REQUESTED,
            AUTH_PASSWORD_RESET_COMPLETED,
            AUTH_EMAIL_VERIFIED,
            AUTH_EMAIL_CHANGE_REQUESTED,
            AUTH_EMAIL_CHANGE_CANCELED,
            AUTH_EMAIL_CHANGED,
            AUTH_OIDC_LOGIN,
            AUTH_OIDC_LINKED,
            AUTH_OIDC_UNLINKED,
            AUTH_PASSKEY_REGISTERED,
            AUTH_PASSKEY_REMOVED,
            AUTH_PASSKEY_LOGIN,
            AUTH_PASSKEY_CLONE_DETECTED,
        ),
    ),
    (
        "Account",
        (
            ACCOUNT_PROFILE_UPDATED,
            ACCOUNT_DELETED,
        ),
    ),
    (
        "Admin",
        (
            ADMIN_USER_DISABLED,
            ADMIN_USER_ENABLED,
            ADMIN_USER_PROMOTED,
            ADMIN_USER_DEMOTED,
            ADMIN_USER_PASSWORD_RESET,
            ADMIN_USER_DELETED,
            ADMIN_SETTINGS_UPDATED,
            ADMIN_SMTP_UPDATED,
            ADMIN_OIDC_PROVIDER_SAVED,
            ADMIN_OIDC_PROVIDER_DELETED,
        ),
    ),
    (
        "Files",
        (
            FILES_UPLOAD_GPS_DETECTED,
            FILES_UPLOAD_GPS_CLEAN,
            FILES_GPS_STRIPPED,
            FILES_GPS_RELEASED,
            FILES_GPS_DISCARDED,
        ),
    ),
)

ALL_ACTIONS: frozenset[str] = frozenset(
    a for _, actions in ACTIONS_BY_DOMAIN for a in actions
)


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return None


def _user_agent(request: Request | None) -> str | None:
    if request is None:
        return None
    ua = request.headers.get("user-agent")
    return ua[:256] if ua else None


async def record(
    db: AsyncSession,
    *,
    action: str,
    request: Request | None = None,
    actor: User | None = None,
    actor_label: str | None = None,
    outcome: str = "success",
    target_type: str | None = None,
    target_id: uuid.UUID | str | None = None,
    target_label: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Persist an audit event. Never raises — failures only log.

    The caller still owns the surrounding transaction; this just `db.add`s
    the row. The caller's commit (or rollback) will decide whether the
    event lands. For events that must survive a rollback (e.g. failed
    login attempts) the caller should commit immediately.
    """
    label = actor_label
    if label is None and actor is not None:
        label = actor.email

    try:
        db.add(
            AuditEvent(
                actor_user_id=actor.id if actor is not None else None,
                actor_label=label[:256] if label else None,
                action=action,
                outcome=outcome,
                target_type=target_type,
                target_id=str(target_id) if target_id is not None else None,
                target_label=target_label[:256] if target_label else None,
                ip=_client_ip(request),
                user_agent=_user_agent(request),
                event_metadata=metadata,
            )
        )
        # Flush so a constraint error surfaces here (and is swallowed) instead
        # of poisoning the caller's transaction at commit time.
        await db.flush()
    except SQLAlchemyError:
        logger.warning("failed to record audit event %s", action, exc_info=True)


async def list_events(
    db: AsyncSession,
    *,
    action_prefix: str | None = None,
    actions: list[str] | None = None,
    actor_user_id: uuid.UUID | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[AuditEvent]:
    stmt = select(AuditEvent).order_by(AuditEvent.created_at.desc())
    if action_prefix:
        stmt = stmt.where(AuditEvent.action.like(f"{action_prefix}%"))
    if actions:
        stmt = stmt.where(AuditEvent.action.in_(actions))
    if actor_user_id is not None:
        stmt = stmt.where(AuditEvent.actor_user_id == actor_user_id)
    stmt = stmt.limit(limit).offset(offset)
    return list((await db.execute(stmt)).scalars().all())


async def purge_older_than_days(db: AsyncSession, days: int) -> int:
    """Optional retention helper for future scheduled cleanup. Returns deleted count."""
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(
        delete(AuditEvent).where(AuditEvent.created_at < cutoff)
    )
    return result.rowcount or 0
