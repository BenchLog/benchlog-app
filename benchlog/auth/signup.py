"""Shared signup field validation used by password and passkey flows."""

import re
import uuid
from dataclasses import dataclass

from email_validator import EmailNotValidError, validate_email
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.auth import users as user_svc

# Usernames are immutable after signup and will appear in public URLs (e.g.
# /u/<username>), so enforce URL-safe slug rules at creation time.
USERNAME_MIN_LEN = 2
USERNAME_MAX_LEN = 32
USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$")
USERNAME_RULES_HINT = (
    "2-32 chars, lowercase letters, numbers, hyphens, and underscores; "
    "must start and end with a letter or number."
)


class SignupValidationError(ValueError):
    pass


class EmailAlreadyRegistered(SignupValidationError):
    """Email collision on signup.

    Raised separately so the password-signup caller can swap to a response
    indistinguishable from a successful signup (to avoid account enumeration).
    Passkey-signup callers treat this as a regular SignupValidationError via
    isinstance, preserving their existing behavior until updated in a later
    pass.
    """
    pass


@dataclass
class SignupFields:
    email: str
    username: str
    display_name: str


@dataclass
class ProfileFields:
    email: str
    display_name: str


def validate_username(value: str) -> str:
    normalized = value.strip().lower()
    if not (USERNAME_MIN_LEN <= len(normalized) <= USERNAME_MAX_LEN):
        raise SignupValidationError(
            f"Username must be {USERNAME_MIN_LEN}-{USERNAME_MAX_LEN} characters."
        )
    if not USERNAME_RE.match(normalized):
        raise SignupValidationError(f"Invalid username. {USERNAME_RULES_HINT}")
    return normalized


async def validate_signup_fields(
    db: AsyncSession, email: str, username: str, display_name: str
) -> SignupFields:
    try:
        email_valid = validate_email(email.strip(), check_deliverability=False)
        email_normalized = email_valid.normalized
    except EmailNotValidError as exc:
        raise SignupValidationError(f"Invalid email: {exc}")

    username_clean = validate_username(username)

    if await user_svc.get_user_by_email_or_pending(db, email_normalized):
        raise EmailAlreadyRegistered("An account with this email already exists.")
    if await user_svc.get_user_by_username(db, username_clean):
        raise SignupValidationError("That username is taken.")

    return SignupFields(
        email=email_normalized,
        username=username_clean,
        display_name=display_name.strip() or username_clean,
    )


async def validate_profile_fields(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    email: str,
    display_name: str,
) -> ProfileFields:
    """Validate edits to an existing user's profile.

    Username is immutable after signup and is not accepted here. Email
    uniqueness ignores the current user so leaving it unchanged is not a
    collision.
    """
    try:
        email_valid = validate_email(email.strip(), check_deliverability=False)
        email_normalized = email_valid.normalized
    except EmailNotValidError as exc:
        raise SignupValidationError(f"Invalid email: {exc}")

    existing_email = await user_svc.get_user_by_email_or_pending(db, email_normalized)
    if existing_email is not None and existing_email.id != user_id:
        raise SignupValidationError("That email is already in use.")

    return ProfileFields(
        email=email_normalized,
        display_name=display_name.strip(),
    )
