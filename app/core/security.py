"""Password hashing and JWT encode/decode.

Imports only `app.core.config` — no models, no database, no FastAPI. That
is deliberate and is why this file was written first: it is the only part
of the auth column fully testable without a database, and nothing here
can grow a hidden dependency on a request or a session.

**This module raises no HTTP errors.** `decode_access_token` lets PyJWT's
`InvalidTokenError` propagate; turning that into a 401 is the boundary
layer's job (Deadline 3, `core/dependencies.py`). A service that knows
about status codes is a service that cannot be called from anywhere else.
"""
import datetime as dt

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import get_settings

_hasher = PasswordHasher()

DUMMY_PASSWORD_HASH = _hasher.hash("not-a-real-password")


def hash_password(plain: str) -> str:
    """Argon2id hash, with the parameters encoded into the string itself.

    That self-describing format is why `scripts/seed.py` could hash its
    three users with a bare `PasswordHasher()` before this file existed:
    `verify()` reads the cost parameters out of the stored hash rather
    than assuming today's settings, so seeded accounts keep working
    whatever is configured here later.
    """
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """False on a wrong password OR a malformed hash. Never raises.

    Both failure modes are caught, and the second one is not obvious:
    `VerifyMismatchError` subclasses `VerificationError` subclasses
    `Argon2Error`, but **`InvalidHashError` subclasses `ValueError`** and
    is not an `Argon2Error` at all (verified in the container, not read
    off a docstring). So `except Argon2Error` — the intuitive single
    clause — would let a corrupt or empty `password_hash` escape as a 500
    from inside the login handler, where the honest answer is "these
    credentials do not authenticate anyone."
    """
    try:
        return _hasher.verify(hashed, plain)
    except (VerificationError, InvalidHashError):
        return False


def create_access_token(user_id: int, role: str) -> str:
    """HS256 token carrying the role as a claim.

    `sub` is **stringified**, and that is load-bearing. PyJWT >= 2.10
    requires a string subject but enforces it on *decode*, not encode: an
    int `sub` produces a perfectly valid-looking token here and then
    raises `InvalidSubjectError` on every subsequent request. The symptom
    would appear in `get_current_user` while the bug lived in this
    function. See `scripts/check_jwt.py`, which asserts exactly this.

    The role travels in the token so `require_role` can authorise without
    a database round-trip per request — which matters at Deadline 5, when
    500 concurrent requests must not add user-lookup noise to the lock
    measurements. Accepted tradeoff: a role change does not take effect
    until the token expires.
    """
    settings = get_settings()
    now = dt.datetime.now(dt.timezone.utc)

    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": now,
        "exp": now + dt.timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Verified claims, or raises `jwt.InvalidTokenError`.

    `exp` is checked by the library rather than by us, which is what
    bounds the stale-role tradeoff above to `ACCESS_TOKEN_EXPIRE_MINUTES`.
    `InvalidTokenError` is the common base of `ExpiredSignatureError`,
    `InvalidSignatureError` and `InvalidSubjectError`, so the caller's
    401 handler is a single `except` clause covering expiry, tampering
    and a malformed subject alike.
    """
    settings = get_settings()
    return jwt.decode(
        token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
    )
