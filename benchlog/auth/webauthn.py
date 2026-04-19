"""WebAuthn registration + authentication helpers.

The Relying Party ID (RP ID) and origin are derived from BENCHLOG_BASE_URL.
Browsers reject WebAuthn over plain HTTP except on `localhost`.
"""

from urllib.parse import urlparse

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from benchlog.config import settings
from benchlog.models import User, WebAuthnCredential


def _parsed_base_url():
    return urlparse(settings.base_url)


def rp_id() -> str:
    host = _parsed_base_url().hostname or "localhost"
    return host


def rp_origin() -> str:
    parsed = _parsed_base_url()
    scheme = parsed.scheme or "http"
    netloc = parsed.netloc or "localhost"
    return f"{scheme}://{netloc}"


def rp_name() -> str:
    return "BenchLog"


def make_registration_options(user: User, existing_credentials: list[WebAuthnCredential]):
    return generate_registration_options(
        rp_id=rp_id(),
        rp_name=rp_name(),
        user_id=user.id.bytes,
        user_name=user.email,
        user_display_name=user.display_name,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=c.credential_id) for c in existing_credentials
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )


def make_signup_registration_options(
    user_id_bytes: bytes, email: str, display_name: str
):
    """Registration options for a user that doesn't exist yet."""
    return generate_registration_options(
        rp_id=rp_id(),
        rp_name=rp_name(),
        user_id=user_id_bytes,
        user_name=email,
        user_display_name=display_name,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )


def make_authentication_options():
    """Discoverable-credential login — browser chooses the credential.

    Any future "remember me" flow that wants to hint specific credentials
    should grow an `allow_credentials` parameter here and plumb it through.
    """
    return generate_authentication_options(
        rp_id=rp_id(),
        user_verification=UserVerificationRequirement.PREFERRED,
        allow_credentials=[],
    )


def verify_registration(credential: dict, expected_challenge: bytes):
    return verify_registration_response(
        credential=credential,
        expected_challenge=expected_challenge,
        expected_origin=rp_origin(),
        expected_rp_id=rp_id(),
    )


def verify_authentication(
    credential: dict,
    expected_challenge: bytes,
    stored: WebAuthnCredential,
):
    return verify_authentication_response(
        credential=credential,
        expected_challenge=expected_challenge,
        expected_origin=rp_origin(),
        expected_rp_id=rp_id(),
        credential_public_key=stored.public_key,
        credential_current_sign_count=stored.sign_count,
    )
