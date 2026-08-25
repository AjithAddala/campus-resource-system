"""Exercise the Deadline 5 exactly-once guarantee against the running API.

Run inside the container:

    docker compose exec app python scripts/check_idempotency.py

Exits non-zero on the first failure. Sixth gate in the project.

Exactly-once is the third guarantee and the only one keyed on the
**request** rather than on a user or a resource, so the assertions are
organised by what a client can actually do wrong:

  same request twice          -> one allocation, identical response
  DIFFERENT request, same key -> 422, and nothing allocated
  same key, different user    -> both succeed (UNIQUE is on the PAIR)
  a request that FAILED       -> no key survives; a retry books cleanly
  N simultaneous retries      -> one allocates, the rest replay

The last one is the only assertion here that needs concurrency, and it is
the only one that would fail against a build with the key insert in its
own transaction -- everything above it passes against a split-commit
implementation, because sequential retries never race. Same lesson as
Benchmark 2 at Deadline 4, applied before writing rather than after: it
runs as TRIALS and counts, because a single run of a sub-millisecond race
is a coin flip and not an instrument.

Leaves the database at its post-seed state: reservations deleted, keys
deleted, `allocated` back to zero.
"""
import collections
import sys
import threading
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.security import hash_password  # noqa: E402
from scripts._db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    GPUCluster,
    GPUReservation,
    IdempotencyKey,
    ReservationStatus,
    ResourceStatus,
    Role,
    User,
)

BASE = "http://localhost:8000/api/v1"
SEED_PASSWORD = "campus123"
TEST_PASSWORD = "check-password-123"

failures = []
made_users: list[int] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{'  -> ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def login(email: str, password: str = SEED_PASSWORD) -> str:
    r = httpx.post(
        f"{BASE}/auth/login", data={"username": email, "password": password}
    )
    r.raise_for_status()
    return r.json()["access_token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def reserve(
    token: str, gpu_id: int, count: int, key: str | None = None
) -> httpx.Response:
    headers = bearer(token)
    if key is not None:
        headers["Idempotency-Key"] = key
    return httpx.post(
        f"{BASE}/gpus/{gpu_id}/reservations",
        json={"gpu_count": count},
        headers=headers,
        timeout=30,
    )


def code_of(r: httpx.Response) -> str | None:
    detail = r.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def new_key() -> str:
    return str(uuid.uuid4())


def make_student() -> tuple[str, str]:
    """One fresh STUDENT account -> (email, bearer token)."""
    db = SessionLocal()
    try:
        email = f"idem-{uuid.uuid4().hex[:12]}@iitk.ac.in"
        user = User(
            name="Idempotency Check",
            email=email,
            password_hash=hash_password(TEST_PASSWORD),
            role=Role.STUDENT,
        )
        db.add(user)
        db.flush()
        made_users.append(user.id)
        db.commit()
    finally:
        db.close()
    return email, login(email, TEST_PASSWORD)


def reset_state() -> None:
    """Back to post-seed: no holds, no keys, nothing allocated or blocked."""
    db = SessionLocal()
    try:
        db.query(GPUReservation).delete(synchronize_session=False)
        db.query(IdempotencyKey).delete(synchronize_session=False)
        for cluster in db.query(GPUCluster).all():
            cluster.allocated = 0
            cluster.status = ResourceStatus.AVAILABLE
        db.commit()
    finally:
        db.close()


def reservation_count() -> int:
    db = SessionLocal()
    try:
        return db.query(GPUReservation).count()
    finally:
        db.close()


def key_row(key: str) -> IdempotencyKey | None:
    db = SessionLocal()
    try:
        return (
            db.query(IdempotencyKey).filter(IdempotencyKey.key == key).one_or_none()
        )
    finally:
        db.close()


def key_count(key: str) -> int:
    db = SessionLocal()
    try:
        return db.query(IdempotencyKey).filter(IdempotencyKey.key == key).count()
    finally:
        db.close()


student = login("student@iitk.ac.in")
faculty = login("faculty@iitk.ac.in")

db = SessionLocal()
try:
    clusters = db.query(GPUCluster).order_by(GPUCluster.id).all()
    c1, c2 = clusters[0].id, clusters[1].id
finally:
    db.close()

reset_state()

r1 = reserve(student, c1, 1)
r2 = reserve(student, c1, 1)
check("no key: first request -> 201", r1.status_code == 201, str(r1.status_code))
check("no key: second request -> 201", r2.status_code == 201, str(r2.status_code))
check("no key: TWO reservations exist", reservation_count() == 2, str(reservation_count()))
check(
    "no key: the two are distinct rows",
    r1.json()["id"] != r2.json()["id"],
    f'{r1.json()["id"]} vs {r2.json()["id"]}',
)

reset_state()
k = new_key()
r1 = reserve(student, c1, 1, key=k)
r2 = reserve(student, c1, 1, key=k)

check("key: first request -> 201", r1.status_code == 201, str(r1.status_code))
check(
    "key: retry replays the ORIGINAL status, not 200",
    r2.status_code == 201,
    str(r2.status_code),
)
check("key: retry body is byte-identical", r1.json() == r2.json(), "")
check("key: exactly ONE reservation", reservation_count() == 1, str(reservation_count()))

row = key_row(k)
check("key: the claim was stored", row is not None, "")
check(
    "key: the stored response was recorded in the same commit",
    row is not None and row.status_code == 201 and row.response_body is not None,
    f"status={row.status_code if row else None}",
)
check(
    "key: stored body matches what was returned",
    row is not None and row.response_body == r1.json(),
    "",
)
check(
    "key: endpoint recorded as the logical operation",
    row is not None and row.endpoint == "gpu.reserve",
    str(row.endpoint if row else None),
)

r3 = reserve(student, c1, 1, key=k)
check("key: a third retry still replays", r3.status_code == 201 and r3.json() == r1.json(), "")
check("key: still exactly ONE reservation", reservation_count() == 1, str(reservation_count()))

r = reserve(student, c1, 2, key=k)
check("same key, different body -> 422", r.status_code == 422, str(r.status_code))
check("same key, different body -> IDEMPOTENCY_KEY_REUSED", code_of(r) == "IDEMPOTENCY_KEY_REUSED", str(code_of(r)))
check("the rejected reuse allocated nothing", reservation_count() == 1, str(reservation_count()))

r = reserve(student, c2, 1, key=k)
check("same key, same body, different cluster -> 422", r.status_code == 422, str(r.status_code))
check("cross-cluster reuse allocated nothing", reservation_count() == 1, str(reservation_count()))

reset_state()
shared = new_key()
rs = reserve(student, c1, 1, key=shared)
rf = reserve(faculty, c1, 1, key=shared)
check("same key string, student -> 201", rs.status_code == 201, str(rs.status_code))
check("same key string, faculty -> 201", rf.status_code == 201, str(rf.status_code))
check("same key string, different users: TWO reservations", reservation_count() == 2, str(reservation_count()))
check(
    "the two callers got different reservations",
    rs.json()["id"] != rf.json()["id"] and rs.json()["user_id"] != rf.json()["user_id"],
    "",
)
check("two key rows share the key string", key_count(shared) == 2, str(key_count(shared)))

reset_state()
r = reserve(student, c1, 2)
check("setup: student holds 2 of 2 units", r.status_code == 201, str(r.status_code))

kf = new_key()
r = reserve(student, c1, 1, key=kf)
check("over quota, with a key -> 409", r.status_code == 409, str(r.status_code))
check("over quota, with a key -> QUOTA_EXCEEDED", code_of(r) == "QUOTA_EXCEEDED", str(code_of(r)))
check(
    "the failed request left NO key row (it rolled back with the transaction)",
    key_count(kf) == 0,
    f"{key_count(kf)} rows",
)

db = SessionLocal()
try:
    held = db.query(GPUReservation).filter(GPUReservation.status == ReservationStatus.ACTIVE).all()
    first = held[0]
    cancel_gpu, cancel_res = first.gpu_cluster_id, first.id
finally:
    db.close()
r = httpx.delete(
    f"{BASE}/gpus/{cancel_gpu}/reservations/{cancel_res}",
    headers=bearer(student),
    timeout=30,
)
check("setup: cancelled the hold", r.status_code == 200, str(r.status_code))

r = reserve(student, c1, 1, key=kf)
check("retrying the failed key now ALLOCATES, not replays", r.status_code == 201, str(r.status_code))
check("the retry created a key row this time", key_count(kf) == 1, str(key_count(kf)))

TRIALS = 15
RETRIES = 8
over_allocated = 0
status_seen: collections.Counter = collections.Counter()
rows_seen: collections.Counter = collections.Counter()
divergent_bodies = 0

for _ in range(TRIALS):
    reset_state()
    email, token = make_student()
    key = new_key()
    barrier = threading.Barrier(RETRIES)
    results: list[httpx.Response] = []
    lock = threading.Lock()

    def _retry() -> None:
        barrier.wait()
        resp = reserve(token, c1, 1, key=key)
        with lock:
            results.append(resp)

    threads = [threading.Thread(target=_retry) for _ in range(RETRIES)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = reservation_count()
    rows_seen[rows] += 1
    if rows > 1:
        over_allocated += 1
    for resp in results:
        status_seen[resp.status_code] += 1

    bodies = [resp.json() for resp in results if resp.status_code == 201]
    if bodies and any(b != bodies[0] for b in bodies):
        divergent_bodies += 1

print()
print(f"    BENCHMARK 3  {RETRIES} simultaneous retries of one key, {TRIALS} trials")
print(f"      reservations per trial : {dict(sorted(rows_seen.items()))}   (must be 1)")
print(f"      status codes returned  : {dict(sorted(status_seen.items()))}")
print(f"      OVER-ALLOCATED TRIALS  : {over_allocated}/{TRIALS}")
print()

check(
    f"concurrent retries: exactly one reservation in all {TRIALS} trials",
    over_allocated == 0,
    f"{over_allocated} trials allocated more than once",
)
check(
    "concurrent retries: every trial landed on exactly 1 row",
    set(rows_seen) == {1},
    str(dict(rows_seen)),
)
check(
    "concurrent retries: all replays returned identical bodies",
    divergent_bodies == 0,
    f"{divergent_bodies} trials returned differing bodies",
)
check("concurrent retries: no 500s", 500 not in status_seen, str(dict(status_seen)))
check(
    "concurrent retries: nothing was refused as reuse",
    422 not in status_seen,
    str(dict(status_seen)),
)

reset_state()
db = SessionLocal()
try:
    db.query(User).filter(User.id.in_(made_users)).delete(synchronize_session=False)
    db.commit()
    check("test users removed", db.query(User).count() == 3, str(db.query(User).count()))
    check("reservations cleaned up", db.query(GPUReservation).count() == 0, "")
    check("idempotency keys cleaned up", db.query(IdempotencyKey).count() == 0, "")
    check(
        "clusters back to 0 allocated, none blocked",
        all(
            c.allocated == 0 and c.status is ResourceStatus.AVAILABLE
            for c in db.query(GPUCluster).all()
        ),
        "",
    )
finally:
    db.close()

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
