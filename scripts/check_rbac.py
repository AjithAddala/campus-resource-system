"""Exercise the Deadline 3 boundary against the running API.

Run inside the container:

    docker compose exec app python scripts/check_rbac.py

Exits non-zero on the first failure, so it is a gate rather than
something to read -- the third script in this project written that way,
and the habit has now caught a stale image (check_jwt) and a half-tested
requirement (check_auth) on their first runs.

What this covers that the other two cannot: `core/dependencies.py` was a
stub returning a hardcoded ADMIN until Deadline 3, so *every* assertion
below about 401 and 403 would have failed against yesterday's build --
which is the point. Deadline 2's checkpoint passed with that stub in
place, because `GET /gpus` returned 200 with a bearer token and also
with no token at all. A route that answers both ways has proved only one
of them.

Before writing an assertion here, the question from Deadline 2's audit
was applied to each one: WHICH implementation would fail this? An
assertion that no plausible wrong build fails is measuring something else.
"""
import datetime as dt
import sys
import uuid
from pathlib import Path

import httpx
import jwt

# Running a file inside scripts/ puts scripts/ on sys.path, not the repo
# root, so `app` would not import. Same job as alembic.ini's prepend_sys_path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from scripts._db import SessionLocal  # noqa: E402
from app.models import GPUCluster, Role, Room, User  # noqa: E402

BASE = "http://localhost:8000/api/v1"
SEED_PASSWORD = "campus123"

failures = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{'  -> ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


s = get_settings()


def login(email: str) -> str:
    r = httpx.post(
        f"{BASE}/auth/login", data={"username": email, "password": SEED_PASSWORD}
    )
    r.raise_for_status()
    return r.json()["access_token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def mint(sub, role: str, minutes: int = 60, secret: str | None = None) -> str:
    """A token this system's own issuer would never produce.

    Used to test the decode path directly: a wrong signature, an expired
    token, an int subject, a subject naming nobody. Going through
    /auth/login could not produce any of these.
    """
    now = dt.datetime.now(dt.timezone.utc)
    return jwt.encode(
        {
            "sub": sub,
            "role": role,
            "iat": now,
            "exp": now + dt.timedelta(minutes=minutes),
        },
        secret or s.JWT_SECRET,
        algorithm=s.JWT_ALGORITHM,
    )


student = login("student@iitk.ac.in")
faculty = login("faculty@iitk.ac.in")
admin = login("admin@iitk.ac.in")

# --- 401: the stub is genuinely gone -----------------------------------
# THE assertion of this deadline. Against the Deadline 1 stub every one
# of these returned 200, because the stub took no token and read no
# header. If any of them ever returns 200 again, authentication has
# stopped being load-bearing and every 403 below is decoration.
r = httpx.get(f"{BASE}/gpus")
check("no token -> 401", r.status_code == 401, str(r.status_code))
check(
    "401 carries WWW-Authenticate",
    r.headers.get("www-authenticate", "").lower().startswith("bearer"),
    str(r.headers.get("www-authenticate")),
)

r = httpx.get(f"{BASE}/gpus", headers=bearer("not-a-jwt"))
check("garbage token -> 401", r.status_code == 401, str(r.status_code))

r = httpx.get(
    f"{BASE}/gpus", headers=bearer(mint("1", "ADMIN", secret="wrong-secret"))
)
check("wrong signature -> 401", r.status_code == 401, str(r.status_code))

r = httpx.get(f"{BASE}/gpus", headers=bearer(mint("1", "ADMIN", minutes=-1)))
check("expired token -> 401", r.status_code == 401, str(r.status_code))

# The PyJWT >= 2.10 trap, from the boundary's side. An int `sub` encodes
# silently, so this token looks valid; InvalidSubjectError is raised on
# decode. It must arrive as a 401, never a 500 -- InvalidSubjectError
# subclasses InvalidTokenError, which is why one except clause suffices.
r = httpx.get(f"{BASE}/gpus", headers=bearer(mint(1, "ADMIN")))
check("int sub -> 401, not 500", r.status_code == 401, str(r.status_code))

# Correctly signed, unexpired, and names a user who does not exist. The
# signature proves the token came from us; it does not prove the row is
# still there. A build that skipped the lookup and trusted the claims
# would 200 here (or 500 on a None user downstream).
r = httpx.get(f"{BASE}/gpus", headers=bearer(mint("999999", "ADMIN")))
check("token for a nonexistent user -> 401", r.status_code == 401, str(r.status_code))

# --- 200: a real token still works -------------------------------------
r = httpx.get(f"{BASE}/gpus", headers=bearer(student))
check("student token -> 200 on a read route", r.status_code == 200, str(r.status_code))
check(
    "read route still returns the seeded clusters",
    r.status_code == 200 and len(r.json()) == 2,
    str(r.status_code),
)

# --- GET /me: a token decodes to a real user carrying a role -----------
r = httpx.get(f"{BASE}/me", headers=bearer(student))
check("GET /me -> 200", r.status_code == 200, str(r.status_code))
me = r.json() if r.status_code == 200 else {}
check(
    "GET /me is the seeded student",
    me.get("email") == "student@iitk.ac.in",
    str(me.get("email")),
)
check("GET /me carries the role", me.get("role") == "STUDENT", str(me.get("role")))
check("GET /me does not expose password_hash", "password_hash" not in me, str(list(me)))

r = httpx.get(f"{BASE}/me")
check("GET /me without a token -> 401", r.status_code == 401, str(r.status_code))

# --- 403: role, and the row count that proves WHEN it fired ------------
db = SessionLocal()
try:
    clusters_before = db.query(GPUCluster).count()
    rooms_before = db.query(Room).count()
finally:
    db.close()

payload = {"name": f"check-{uuid.uuid4().hex[:8]}", "gpu_count": 4}
room_payload = {
    "name": f"check-{uuid.uuid4().hex[:8]}",
    "building": "Check Hall",
    "capacity": 20,
}

# No token on a write route must be 401, not 403. The distinction is the
# whole of ARCHITECTURE_AND_WORKFLOWS.md section 1: authentication answers
# "who are you", authorization answers "may you". Collapsing them would
# tell an anonymous caller they are forbidden rather than unidentified.
r = httpx.post(f"{BASE}/gpus", json=payload)
check(
    "POST /gpus with no token -> 401 (not 403)", r.status_code == 401, str(r.status_code)
)

r = httpx.post(f"{BASE}/gpus", json=payload, headers=bearer(student))
check(
    "THE CHECKPOINT: student -> 403 on POST /gpus",
    r.status_code == 403,
    str(r.status_code),
)

r = httpx.post(f"{BASE}/gpus", json=payload, headers=bearer(faculty))
check("faculty -> 403 on POST /gpus", r.status_code == 403, str(r.status_code))

r = httpx.post(f"{BASE}/rooms", json=room_payload, headers=bearer(student))
check("student -> 403 on POST /rooms", r.status_code == 403, str(r.status_code))

# The assertion the status codes above cannot make. `require_role` is a
# dependency, so FastAPI resolves it BEFORE the handler body runs and no
# row can have been written. An implementation that checked the role with
# an `if` at the TOP of the handler would also return 403 and would also
# pass every check above -- and would fail this one the moment the check
# sat one line below the INSERT. Status codes are what the server said;
# the row count is what happened.
db = SessionLocal()
try:
    clusters_after_403 = db.query(GPUCluster).count()
    rooms_after_403 = db.query(Room).count()
finally:
    db.close()
check(
    "403 left no cluster behind (fired before the handler)",
    clusters_after_403 == clusters_before,
    f"{clusters_before} -> {clusters_after_403}",
)
check(
    "403 left no room behind",
    rooms_after_403 == rooms_before,
    f"{rooms_before} -> {rooms_after_403}",
)

# --- the role is read from the DATABASE, not from the claim ------------
# A correctly signed token for the seeded STUDENT, whose `role` claim
# says ADMIN. Only this system could have signed it, so a build that
# authorises on the claim -- which is what DECISIONS.md originally
# specified -- returns 201 and creates a cluster. Reading the role off
# the loaded User row is what makes it a 403. This is the assertion for
# the Deadline 3 reversal recorded in DECISIONS.md.
db = SessionLocal()
try:
    student_row = db.query(User).filter(User.email == "student@iitk.ac.in").one()
    student_id = student_row.id
    check(
        "seeded student really is a STUDENT in the DB",
        student_row.role is Role.STUDENT,
        str(student_row.role),
    )
finally:
    db.close()

r = httpx.post(
    f"{BASE}/gpus", json=payload, headers=bearer(mint(str(student_id), "ADMIN"))
)
check(
    "forged ADMIN claim on a STUDENT row -> 403", r.status_code == 403, str(r.status_code)
)

db = SessionLocal()
try:
    after_forge = db.query(GPUCluster).count()
finally:
    db.close()
check("the forged claim created nothing", after_forge == clusters_before, str(after_forge))

# --- 201: admin may actually do the thing ------------------------------
# Without this, every 403 above would also pass against a route that
# rejects EVERYONE, which is not authorization -- it is an outage.
r = httpx.post(f"{BASE}/gpus", json=payload, headers=bearer(admin))
check("admin -> 201 on POST /gpus", r.status_code == 201, str(r.status_code))
created = r.json() if r.status_code == 201 else {}
check(
    "new cluster starts with allocated = 0",
    created.get("allocated") == 0,
    str(created.get("allocated")),
)

r = httpx.post(f"{BASE}/rooms", json=room_payload, headers=bearer(admin))
check("admin -> 201 on POST /rooms", r.status_code == 201, str(r.status_code))
created_room = r.json() if r.status_code == 201 else {}

# Joined-table inheritance: creating through the subclass must have
# written the `resources` row too and set the discriminator. If it did
# not, GET /gpus/{id} would 404 on a cluster that exists.
if created.get("id"):
    r = httpx.get(f"{BASE}/gpus/{created['id']}", headers=bearer(admin))
    check(
        "created cluster is readable (both inheritance halves written)",
        r.status_code == 200,
        str(r.status_code),
    )

# --- validation still runs, and AFTER authorization --------------------
r = httpx.post(f"{BASE}/gpus", json={"name": "x", "gpu_count": 0}, headers=bearer(admin))
check("gpu_count = 0 -> 422", r.status_code == 422, str(r.status_code))
# A student sending the SAME invalid body must get 403, not 422: the
# dependency chain is authentication -> authorization -> validation, and
# a 422 here would mean the body was parsed for a caller with no business
# reaching the endpoint.
r = httpx.post(
    f"{BASE}/gpus", json={"name": "x", "gpu_count": 0}, headers=bearer(student)
)
check("student + invalid body -> 403, not 422", r.status_code == 403, str(r.status_code))

# --- the /docs Authorize button is wired -------------------------------
# The payoff for login being form-encoded. If the security scheme is
# missing from the schema, the button is absent and the Deadline 10 demo
# goes back to pasting headers into every request.
spec = httpx.get("http://localhost:8000/openapi.json").json()
schemes = spec.get("components", {}).get("securitySchemes", {})
check("openapi declares an OAuth2 security scheme", bool(schemes), str(list(schemes)))
flow_url = ""
for scheme in schemes.values():
    flow_url = scheme.get("flows", {}).get("password", {}).get("tokenUrl", "")
check("tokenUrl points at the login route", flow_url.endswith("api/v1/auth/login"), flow_url)
check(
    "protected route declares the scheme",
    bool(spec["paths"]["/api/v1/gpus"]["get"].get("security")),
    str(spec["paths"]["/api/v1/gpus"]["get"].get("security")),
)

# --- cleanup -----------------------------------------------------------
# A passing run leaves the database at its post-seed counts, so the next
# person to count rows is not misled by this script's leftovers.
db = SessionLocal()
try:
    for model, made in ((GPUCluster, created), (Room, created_room)):
        if made.get("id"):
            row = db.get(model, made["id"])
            if row is not None:
                db.delete(row)
    db.commit()
    check(
        "cluster count back to post-seed",
        db.query(GPUCluster).count() == clusters_before,
        str(db.query(GPUCluster).count()),
    )
    check(
        "room count back to post-seed",
        db.query(Room).count() == rooms_before,
        str(db.query(Room).count()),
    )
finally:
    db.close()

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
