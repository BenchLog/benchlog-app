import bcrypt


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


# Pre-computed at import time so login responses take the same ~100ms
# bcrypt cost regardless of whether the submitted identifier matches a user.
# Without this, attackers can use timing to enumerate valid accounts.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


def dummy_verify() -> None:
    """Perform a bcrypt comparison against a throwaway hash.

    Call this on the "user not found / no password set" branch of login to
    keep response time indistinguishable from a failed password check against
    a real account.
    """
    try:
        bcrypt.checkpw(b"dummy-password-for-timing-check", _DUMMY_HASH.encode("utf-8"))
    except ValueError:
        pass
