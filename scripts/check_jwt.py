"""Verify the PyJWT swap, against the app's real settings.

Run inside the container:

    docker compose exec app python scripts/check_jwt.py

Exits non-zero on the first failure, so it is usable as a gate. Checks the
two things a `pip show` cannot: that jose is genuinely gone from the image,
and that HS256 encode/decode behaves the way `core/security.py` will assume.
"""
import datetime as dt
import sys
from pathlib import Path

import jwt

# Running a file inside scripts/ puts scripts/ on sys.path, not the repo
# root, so `app` would not import. Same job as alembic.ini's prepend_sys_path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402

failures = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{'  -> ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


s = get_settings()
now = dt.datetime.now(dt.timezone.utc)

# 1. jose is gone from the image, not merely unpinned in requirements.txt.
try:
    import jose  # noqa: F401
    check("python-jose absent", False, "still importable")
except ModuleNotFoundError:
    check("python-jose absent", True, "ModuleNotFoundError")

check("PyJWT version", jwt.__version__.startswith("2.10"), jwt.__version__)

# 2. Round-trip with the settings security.py will actually use.
payload = {
    "sub": "1",
    "role": "ADMIN",
    "iat": now,
    "exp": now + dt.timedelta(minutes=s.ACCESS_TOKEN_EXPIRE_MINUTES),
}
token = jwt.encode(payload, s.JWT_SECRET, algorithm=s.JWT_ALGORITHM)
decoded = jwt.decode(token, s.JWT_SECRET, algorithms=[s.JWT_ALGORITHM])
check("encode returns str", isinstance(token, str), type(token).__name__)
check("role survives round-trip", decoded.get("role") == "ADMIN", str(decoded))

# 3. A tampered or wrongly-signed token must be rejected.
try:
    jwt.decode(token, "wrong-secret", algorithms=[s.JWT_ALGORITHM])
    check("wrong secret rejected", False, "accepted!")
except jwt.InvalidSignatureError as e:
    check("wrong secret rejected", True, type(e).__name__)

# 4. Expiry is enforced by the library, not by us.
expired = jwt.encode(
    {"sub": "1", "exp": now - dt.timedelta(seconds=1)},
    s.JWT_SECRET,
    algorithm=s.JWT_ALGORITHM,
)
try:
    jwt.decode(expired, s.JWT_SECRET, algorithms=[s.JWT_ALGORITHM])
    check("expired token rejected", False, "accepted!")
except jwt.ExpiredSignatureError as e:
    check("expired token rejected", True, type(e).__name__)

# 5. PyJWT >= 2.10 requires `sub` to be a string, and enforces it on
#    DECODE, not encode. An int sub therefore encodes happily and blows up
#    on every later request — login looks fine, the whole app 401s. This is
#    the behaviour change most likely to bite security.py: str(user.id) on
#    the way out, int(payload["sub"]) on the way back.
int_sub = jwt.encode({"sub": 1}, s.JWT_SECRET, algorithm=s.JWT_ALGORITHM)
check("int sub encodes silently", isinstance(int_sub, str), "no error at encode")
try:
    jwt.decode(int_sub, s.JWT_SECRET, algorithms=[s.JWT_ALGORITHM])
    check("int sub rejected at decode", False, "accepted — str(user.id) not enforced")
except jwt.exceptions.InvalidSubjectError as e:
    check("int sub rejected at decode", True, str(e))

# 6. One `except` must cover expiry, tampering and a bad subject — that is
#    what lets the 401 handler be a single clause. Note InvalidSubjectError
#    is only in jwt.exceptions; it is not re-exported at jwt.* like the rest.
check(
    "InvalidTokenError is the common base",
    all(
        issubclass(exc, jwt.InvalidTokenError)
        for exc in (
            jwt.ExpiredSignatureError,
            jwt.InvalidSignatureError,
            jwt.exceptions.InvalidSubjectError,
        )
    ),
)

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
