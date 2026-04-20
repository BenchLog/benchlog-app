import os

import bcrypt

# Bcrypt work factor. Production uses the library default (12, ~250ms/hash).
# Tests set BENCHLOG_BCRYPT_ROUNDS=4 in conftest.py (~4ms/hash) — with ~400
# tests creating ~1.5 users each, that's the difference between ~3 min and
# ~3 s of hashing. Still real bcrypt, still valid hashes; just cheap.
_ROUNDS = int(os.environ.get("BENCHLOG_BCRYPT_ROUNDS", "12"))


def hash_password(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=_ROUNDS)
    ).decode("utf-8")


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
