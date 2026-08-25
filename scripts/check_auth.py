"""Exercise the Deadline 2 auth column against the running API.

Run inside the container:

    docker compose exec app python scripts/check_auth.py

Exits non-zero on the first failure, so it is a gate rather than
something to read — the same reason `scripts/check_jwt.py` is a script
and not a paste into a shell. That one caught a stale image on its first
run on a second machine; this one covers what it cannot, which is
everything that only breaks once a database and a router are involved.

Registers under a random email so it is re-runnable without `--reset`,
and deletes that user at the end, so a passing run leaves the database
exactly at its post-seed counts.
"""
import collections
import sys
import threading
import uuid
from pathlib import Path

import httpx
import jwt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from scripts._db import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402

BASE = "http://localhost:8000/api/v1"
SEED_PASSWORD = "campus123"

failures = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{'  -> ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


s = get_settings()
email = f"check-{uuid.uuid4().hex[:12]}@iitk.ac.in"
password = "check-password-123"

r = httpx.post(
    f"{BASE}/auth/register",
    json={"name": "Check User", "email": email, "password": password, "role": "STUDENT"},
)
check("register -> 201", r.status_code == 201, str(r.status_code))
body = r.json() if r.status_code == 201 else {}
check("register echoes role", body.get("role") == "STUDENT", str(body.get("role")))
check("password_hash not exposed", "password_hash" not in body, str(list(body)))
new_user_id = body.get("id")

r = httpx.post(
    f"{BASE}/auth/register",
    json={"name": "Check User", "email": email, "password": password, "role": "STUDENT"},
)
check("duplicate email -> 409", r.status_code == 409, str(r.status_code))
detail = r.json().get("detail", {}) if r.status_code == 409 else {}
check(
    "409 carries a machine-readable code",
    isinstance(detail, dict) and detail.get("code") == "EMAIL_ALREADY_REGISTERED",
    str(detail),
)

race_email = f"race-{uuid.uuid4().hex[:12]}@iitk.ac.in"
RACERS = 8
_barrier = threading.Barrier(RACERS)
_results: list[tuple[int, str | None]] = []
_lock = threading.Lock()


def _race_attempt() -> None:
    payload = {
        "name": "Race",
        "email": race_email,
        "password": password,
        "role": "STUDENT",
    }
    _barrier.wait()
    resp = httpx.post(f"{BASE}/auth/register", json=payload, timeout=30)
    code = resp.json().get("detail", {}).get("code") if resp.status_code == 409 else None
    with _lock:
        _results.append((resp.status_code, code))


_threads = [threading.Thread(target=_race_attempt) for _ in range(RACERS)]
for _t in _threads:
    _t.start()
for _t in _threads:
    _t.join()

tally = collections.Counter(_results)
check(
    f"{RACERS} simultaneous duplicates -> exactly one 201",
    tally[(201, None)] == 1,
    str(dict(tally)),
)
check(
    f"the other {RACERS - 1} -> 409, no 500s",
    tally[(409, "EMAIL_ALREADY_REGISTERED")] == RACERS - 1,
    str(dict(tally)),
)

r = httpx.post(
    f"{BASE}/auth/register",
    json={"name": "x", "email": "someone@iitk.ac.in", "password": "short", "role": "STUDENT"},
)
check("password under 8 chars -> 422", r.status_code == 422, str(r.status_code))

r = httpx.post(
    f"{BASE}/auth/register",
    json={"name": "x", "email": "someone@iitk.ac.in", "password": password, "role": "WIZARD"},
)
check("unknown role -> 422", r.status_code == 422, str(r.status_code))

r = httpx.post(f"{BASE}/auth/login", data={"username": email, "password": password})
check("login -> 200", r.status_code == 200, str(r.status_code))
token_body = r.json() if r.status_code == 200 else {}
check("token_type is bearer", token_body.get("token_type") == "bearer", str(token_body.get("token_type")))
token = token_body.get("access_token", "")

claims = {}
if token:
    claims = jwt.decode(token, s.JWT_SECRET, algorithms=[s.JWT_ALGORITHM])
check("token decodes", bool(claims), str(sorted(claims)))
check("role is a claim in the token", claims.get("role") == "STUDENT", str(claims.get("role")))
check("sub is a string", isinstance(claims.get("sub"), str), repr(claims.get("sub")))
check(
    "sub is the registered user's id",
    claims.get("sub") == str(new_user_id),
    f"{claims.get('sub')} vs {new_user_id}",
)
check("exp present", "exp" in claims and "iat" in claims, str(sorted(claims)))

r = httpx.post(f"{BASE}/auth/login", data={"username": email, "password": "wrong-password"})
check("wrong password -> 401", r.status_code == 401, str(r.status_code))
wrong_pw_body = r.text

r = httpx.post(f"{BASE}/auth/login", data={"username": "nobody@iitk.ac.in", "password": password})
check("unknown email -> 401", r.status_code == 401, str(r.status_code))
check("both 401s are indistinguishable", r.text == wrong_pw_body, r.text[:60])

r = httpx.post(f"{BASE}/auth/login", json={"username": email, "password": password})
check("JSON body to /login -> 422", r.status_code == 422, str(r.status_code))

r = httpx.post(
    f"{BASE}/auth/login",
    data={"username": "student@iitk.ac.in", "password": SEED_PASSWORD},
)
check("seeded student logs in", r.status_code == 200, str(r.status_code))
if r.status_code == 200:
    seed_claims = jwt.decode(
        r.json()["access_token"], s.JWT_SECRET, algorithms=[s.JWT_ALGORITHM]
    )
    check("seeded student's role is STUDENT", seed_claims.get("role") == "STUDENT", str(seed_claims))

r = httpx.post(
    f"{BASE}/auth/login",
    data={"username": "faculty@iitk.ac.in", "password": SEED_PASSWORD},
)
if r.status_code == 200:
    faculty_claims = jwt.decode(
        r.json()["access_token"], s.JWT_SECRET, algorithms=[s.JWT_ALGORITHM]
    )
    check(
        "seeded faculty's role is FACULTY",
        faculty_claims.get("role") == "FACULTY",
        str(faculty_claims.get("role")),
    )
else:
    check("seeded faculty logs in", False, str(r.status_code))

db = SessionLocal()
try:
    racers_created = db.query(User).filter(User.email == race_email).count()
    check("race created exactly one row", racers_created == 1, f"{racers_created} rows")

    deleted = db.query(User).filter(User.email.in_([email, race_email])).delete(
        synchronize_session=False
    )
    db.commit()
    check("test users cleaned up", deleted == 2, f"{deleted} rows deleted")
finally:
    db.close()

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
