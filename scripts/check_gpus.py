"""Exercise the Deadline 4 GPU allocation transaction against the running API.

Run inside the container:

    docker compose exec app python scripts/check_gpus.py

Exits non-zero on the first failure. Fifth gate in the project.

This is the flagship path, so the assertions are organised by WHICH
GUARANTEE each one is about, not by endpoint:

  CAPACITY  keyed on the RESOURCE  -> the cluster row lock
  QUOTA     keyed on the USER      -> the user row lock
  (exactly-once, keyed on the REQUEST, arrives at Deadline 5)

The two that matter most are at the bottom, under CONCURRENCY. Everything
above them passes against a transaction with NO LOCKS AT ALL, because
sequential requests never race -- which is exactly why a checkpoint
phrased as "2 GPUs reserve, a 3rd returns QUOTA_EXCEEDED" cannot on its
own prove anything about locking.

Leaves the database at its post-seed state: reservations deleted,
`allocated` back to zero, no cluster left BLOCKED.
"""
import collections
import sys
import threading
import uuid
from pathlib import Path

import httpx

# Running a file inside scripts/ puts scripts/ on sys.path, not the repo
# root, so `app` would not import. Same job as alembic.ini's prepend_sys_path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from scripts._db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    GPUCluster,
    GPUReservation,
    ReservationStatus,
    ResourceStatus,
    Role,
    Room,
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


def make_students(n: int) -> list[str]:
    """n fresh STUDENT accounts, returned as bearer tokens.

    The capacity race needs DISTINCT users: capacity is keyed on the
    resource, so if every racer shares an account the user-row lock
    serializes them and the cluster gate is never contended at all.
    """
    db = SessionLocal()
    emails = []
    try:
        hashed = hash_password(TEST_PASSWORD)
        for _ in range(n):
            email = f"check-{uuid.uuid4().hex[:12]}@iitk.ac.in"
            user = User(
                name="Check Student",
                email=email,
                password_hash=hashed,
                role=Role.STUDENT,
            )
            db.add(user)
            db.flush()
            made_users.append(user.id)
            emails.append(email)
        db.commit()
    finally:
        db.close()
    return [login(e, TEST_PASSWORD) for e in emails]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def reserve(token: str, gpu_id: int, count: int) -> httpx.Response:
    return httpx.post(
        f"{BASE}/gpus/{gpu_id}/reservations",
        json={"gpu_count": count},
        headers=bearer(token),
        timeout=30,
    )


def code_of(r: httpx.Response) -> str | None:
    detail = r.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def reset_state() -> None:
    """Back to post-seed: no holds, nothing allocated, nothing blocked."""
    db = SessionLocal()
    try:
        db.query(GPUReservation).delete(synchronize_session=False)
        for cluster in db.query(GPUCluster).all():
            cluster.allocated = 0
            cluster.status = ResourceStatus.AVAILABLE
        db.commit()
    finally:
        db.close()


def cluster_state(gpu_id: int) -> tuple[int, int]:
    db = SessionLocal()
    try:
        c = db.get(GPUCluster, gpu_id)
        return c.allocated, c.gpu_count
    finally:
        db.close()


def held_by(email: str) -> int:
    """Units held, computed the same way the quota gate computes them."""
    db = SessionLocal()
    try:
        from app.models import User

        uid = db.query(User).filter(User.email == email).one().id
        rows = (
            db.query(GPUReservation)
            .filter(
                GPUReservation.user_id == uid,
                GPUReservation.status == ReservationStatus.ACTIVE,
            )
            .all()
        )
        return sum(r.gpu_count for r in rows)
    finally:
        db.close()


settings = get_settings()
LOCK_OFF = settings.BENCHMARK_UNSAFE_NO_USER_LOCK

print(
    "user-row lock: "
    + ("DISABLED  <-- BROKEN BUILD, Benchmark 2 baseline" if LOCK_OFF else "ENABLED")
)
print()

student = login("student@iitk.ac.in")
faculty = login("faculty@iitk.ac.in")
admin = login("admin@iitk.ac.in")

db = SessionLocal()
try:
    clusters = db.query(GPUCluster).order_by(GPUCluster.id).all()
    c1, c2 = clusters[0].id, clusters[1].id
    c1_units = clusters[0].gpu_count
    room_id = db.query(Room).order_by(Room.id).first().id
finally:
    db.close()

reset_state()

# --- the boundary -------------------------------------------------------
r = httpx.post(f"{BASE}/gpus/{c1}/reservations", json={"gpu_count": 1})
check("no token -> 401", r.status_code == 401, str(r.status_code))

# --- CAPACITY: keyed on the resource ------------------------------------
r = reserve(student, c1, 2)
check("student reserves 2 units -> 201", r.status_code == 201, str(r.status_code))
first_hold = r.json() if r.status_code == 201 else {}
check("hold is ACTIVE", first_hold.get("status") == "ACTIVE", str(first_hold.get("status")))

allocated, total = cluster_state(c1)
check("counter incremented under the lock", allocated == 2, f"allocated={allocated}")

r = httpx.get(f"{BASE}/gpus/{c1}/availability", headers=bearer(student))
body = r.json()
check(
    "availability agrees with the counter",
    body["allocated"] == 2 and body["free"] == total - 2,
    str(body),
)

# --- QUOTA: keyed on the user -------------------------------------------
# THE CHECKPOINT. Note this passes with no user lock at all -- sequential
# requests do not race. It proves the arithmetic, not the locking.
r = reserve(student, c1, 1)
check(
    "THE CHECKPOINT: 3rd unit -> 409 QUOTA_EXCEEDED",
    r.status_code == 409 and code_of(r) == "QUOTA_EXCEEDED",
    f"{r.status_code} {code_of(r)}",
)

# The cross-cluster case, sequentially. A DIFFERENT cluster row, so the
# cluster lock never contends -- only the quota gate can refuse this.
r = reserve(student, c2, 2)
check(
    "2 more on a DIFFERENT cluster -> 409 QUOTA_EXCEEDED",
    r.status_code == 409 and code_of(r) == "QUOTA_EXCEEDED",
    f"{r.status_code} {code_of(r)}",
)
check("nothing allocated on the second cluster", cluster_state(c2)[0] == 0, str(cluster_state(c2)))

# Quota is per role, not global: FACULTY is 10 and ADMIN is unlimited.
r = reserve(faculty, c1, 4)
check("faculty (quota 10) reserves 4 -> 201", r.status_code == 201, str(r.status_code))

r = reserve(admin, c1, c1_units - 6)
check(
    "admin (max_units NULL = unlimited) fills the cluster -> 201",
    r.status_code == 201,
    str(r.status_code),
)
allocated, total = cluster_state(c1)
check("cluster is now exactly full", allocated == total, f"{allocated}/{total}")

# --- CAPACITY refusal, and it must not be QUOTA_EXCEEDED ----------------
# Distinct codes because the remedies differ: wait / try another cluster,
# versus release something you hold. The admin is unlimited, so a quota
# code here would be flatly wrong.
r = reserve(admin, c1, 1)
check(
    "full cluster -> 409 CAPACITY_EXHAUSTED (not QUOTA_EXCEEDED)",
    r.status_code == 409 and code_of(r) == "CAPACITY_EXHAUSTED",
    f"{r.status_code} {code_of(r)}",
)
check("refusal left the counter alone", cluster_state(c1)[0] == total, str(cluster_state(c1)))

# --- the gates in the right order ---------------------------------------
# A STUDENT at quota, asking a FULL cluster, must hear about the quota:
# the user lock and its gate come first, so the request never reaches the
# capacity check. If this ever says CAPACITY_EXHAUSTED, the gates have
# been reordered and the lock order with them.
r = reserve(student, c1, 1)
check(
    "over-quota caller on a full cluster hears QUOTA first",
    r.status_code == 409 and code_of(r) == "QUOTA_EXCEEDED",
    f"{r.status_code} {code_of(r)}",
)

# --- reconciliation: the counter and the rows agree ---------------------
db = SessionLocal()
try:
    summed = sum(
        row.gpu_count
        for row in db.query(GPUReservation).filter(
            GPUReservation.gpu_cluster_id == c1,
            GPUReservation.status == ReservationStatus.ACTIVE,
        )
    )
finally:
    db.close()
check(
    "allocated == SUM(active reservations)",
    summed == cluster_state(c1)[0],
    f"sum={summed} allocated={cluster_state(c1)[0]}",
)

# --- validation and routing ---------------------------------------------
r = reserve(student, c1, 0)
check("gpu_count = 0 -> 422", r.status_code == 422, str(r.status_code))
r = reserve(student, c1, -2)
check("negative gpu_count -> 422", r.status_code == 422, str(r.status_code))
r = reserve(student, room_id, 1)
check("reserving a ROOM through /gpus -> 404", r.status_code == 404, str(r.status_code))
r = reserve(student, 999999, 1)
check("nonexistent cluster -> 404", r.status_code == 404, str(r.status_code))

# --- BLOCKED, item 6 ratified at Deadline 3 -----------------------------
reset_state()
db = SessionLocal()
try:
    db.get(GPUCluster, c2).status = ResourceStatus.BLOCKED
    db.commit()
finally:
    db.close()

r = reserve(student, c2, 1)
check(
    "blocked cluster -> 409 RESOURCE_BLOCKED",
    r.status_code == 409 and code_of(r) == "RESOURCE_BLOCKED",
    f"{r.status_code} {code_of(r)}",
)
r = reserve(admin, c2, 1)
check("admin is refused on a blocked cluster too", r.status_code == 409, str(r.status_code))
reset_state()

# --- CANCEL: owner-or-admin, and naturally idempotent -------------------
r = reserve(student, c1, 2)
hold = r.json()
check("student holds 2 again after reset", r.status_code == 201, str(r.status_code))
check("held units = 2", held_by("student@iitk.ac.in") == 2, str(held_by("student@iitk.ac.in")))

# Item 8, ratified this deadline: the resource id in the path is
# load-bearing. The same reservation id under the WRONG cluster must 404,
# or the path segment is decorative and the ambiguity is back.
r = httpx.delete(f"{BASE}/gpus/{c2}/reservations/{hold['id']}", headers=bearer(student))
check("cancel under the wrong cluster id -> 404", r.status_code == 404, str(r.status_code))

r = httpx.delete(f"{BASE}/gpus/{c1}/reservations/{hold['id']}", headers=bearer(faculty))
check("cancelling someone else's hold -> 403", r.status_code == 403, str(r.status_code))
check("the 403 did not cancel it", cluster_state(c1)[0] == 2, str(cluster_state(c1)))

r = httpx.delete(f"{BASE}/gpus/{c1}/reservations/{hold['id']}", headers=bearer(student))
check("owner cancels -> 200", r.status_code == 200, str(r.status_code))
check("status is CANCELLED", r.json().get("status") == "CANCELLED", str(r.json().get("status")))
check("counter decremented", cluster_state(c1)[0] == 0, str(cluster_state(c1)))
check("held units back to 0", held_by("student@iitk.ac.in") == 0, str(held_by("student@iitk.ac.in")))

# Cancelling twice is a no-op, NOT a double decrement. This is the direct
# payoff of recomputing held units by SUM instead of keeping a counter:
# with a counter, this line would corrupt it and only show up later, as a
# valid allocation somewhere else being wrongly refused.
r = httpx.delete(f"{BASE}/gpus/{c1}/reservations/{hold['id']}", headers=bearer(student))
check("cancelling twice -> 200, same row", r.status_code == 200, str(r.status_code))
check("counter NOT double-decremented", cluster_state(c1)[0] == 0, str(cluster_state(c1)))

# Releasing frees the quota again -- the point of hold-until-release.
r = reserve(student, c1, 2)
check("student may allocate again after releasing", r.status_code == 201, str(r.status_code))

r = httpx.delete(
    f"{BASE}/gpus/{c1}/reservations/{r.json()['id']}", headers=bearer(admin)
)
check("ADMIN may cancel another user's hold -> 200", r.status_code == 200, str(r.status_code))
check("counter decremented by the admin cancel", cluster_state(c1)[0] == 0, str(cluster_state(c1)))

# =======================================================================
# CONCURRENCY. Everything above this line passes against a transaction
# with no locks whatsoever.
# =======================================================================
reset_state()

# --- BENCHMARK 2 (quota): the argument this project is built on --------
# ONE student, quota 2, two SIMULTANEOUS 2-unit requests at DIFFERENT
# clusters. The cluster rows never contend -- different rows -- so the
# capacity gate is satisfied twice over and both requests are correct as
# far as any resource is concerned.
#
#   without the user lock: both read held=0, both pass, held becomes 4
#   with it:               one waits, re-reads held=2, is refused
#
# The lock was never missing. It was the WRONG LOCK for that invariant.
# ONE trial is not an instrument. The first run of this benchmark against
# the UNLOCKED build reported held = 2 -- i.e. it passed, against the very
# build it exists to indict. The corruption window between "SUM held
# units" and COMMIT is well under a millisecond, and two HTTP requests
# released on a barrier do not reliably land inside it.
#
# That is this project's own lesson arriving from a new direction: a test
# that passes against the broken implementation is not a test. The fix is
# to make it a measurement rather than a single assertion -- run the race
# many times and count how often the invariant broke. `corrupt` is then a
# number that separates the two builds instead of a coin flip.
TRIALS = 25
corrupt = 0
held_seen: collections.Counter = collections.Counter()
succeeded_seen: collections.Counter = collections.Counter()

for _ in range(TRIALS):
    reset_state()
    barrier = threading.Barrier(2)
    trial_results: list[tuple[int, str | None]] = []
    trial_lock = threading.Lock()

    def _quota_race(cluster_id: int) -> None:
        barrier.wait()
        resp = reserve(student, cluster_id, 2)
        with trial_lock:
            trial_results.append((resp.status_code, code_of(resp)))

    threads = [threading.Thread(target=_quota_race, args=(cid,)) for cid in (c1, c2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    held = held_by("student@iitk.ac.in")
    held_seen[held] += 1
    succeeded_seen[sum(1 for code, _ in trial_results if code == 201)] += 1
    if held > 2:
        corrupt += 1

print()
print(f"    BENCHMARK 2  cross-cluster quota race, {TRIALS} trials")
print(f"      held units observed : {dict(sorted(held_seen.items()))}   (limit 2)")
print(f"      requests succeeding : {dict(sorted(succeeded_seen.items()))}")
print(f"      OVER-QUOTA TRIALS   : {corrupt}/{TRIALS}")
print()

check(
    f"cross-cluster race: zero over-quota trials in {TRIALS}",
    corrupt == 0,
    f"{corrupt} trials ended with held > 2",
)
check(
    "cross-cluster race: never more than one success in a trial",
    set(succeeded_seen) <= {1},
    str(dict(succeeded_seen)),
)

# --- capacity under concurrency ----------------------------------------
# The other invariant, and the one the cluster lock DOES protect.
#
# **Every racer is a DIFFERENT user, and that is the whole point.** The
# first version of this race used one ADMIN account for all 12 requests,
# so the USER row lock serialized them and the cluster gate was never
# actually contended -- it passed against a build whose capacity gate
# read a stale `allocated`. The identical bug in the course path was
# caught only because that race used distinct students. Capacity is keyed
# on the RESOURCE, so the racers must differ in everything except the
# resource.
#
# `allocated` must land exactly on the limit, never past it.
# `gpu_capacity_sane` would reject an overshoot as a 500, so a clean run
# here is also evidence that no request had to be saved by the CHECK.
reset_state()
RACERS = 12
cap_tokens = make_students(RACERS)
_barrier2 = threading.Barrier(RACERS)
_cap_results: list[int] = []
_lock2 = threading.Lock()


def _capacity_race(token: str) -> None:
    _barrier2.wait()
    # 1 unit each, so a STUDENT's quota of 2 never refuses anyone: the
    # only gate that may say no here is capacity.
    resp = reserve(token, c1, 1)
    with _lock2:
        _cap_results.append(resp.status_code)


threads = [threading.Thread(target=_capacity_race, args=(t,)) for t in cap_tokens]
for t in threads:
    t.start()
for t in threads:
    t.join()

allocated, total = cluster_state(c1)
cap_tally = dict(collections.Counter(_cap_results))
print()
print(
    f"    CAPACITY RACE  {RACERS} concurrent 1-unit requests, {total} units: "
    f"{cap_tally}, allocated = {allocated}"
)
print()
check(
    "concurrent capacity: allocated never exceeds total",
    allocated <= total,
    f"{allocated}/{total}",
)
check(
    f"concurrent capacity: exactly {total} succeeded",
    cap_tally.get(201, 0) == total,
    str(cap_tally),
)
check("concurrent capacity: no 500s", 500 not in cap_tally, str(cap_tally))

db = SessionLocal()
try:
    summed = sum(
        row.gpu_count
        for row in db.query(GPUReservation).filter(
            GPUReservation.gpu_cluster_id == c1,
            GPUReservation.status == ReservationStatus.ACTIVE,
        )
    )
finally:
    db.close()
check(
    "after the race, allocated == SUM(active reservations)",
    summed == allocated,
    f"sum={summed} allocated={allocated}",
)

# --- cleanup ------------------------------------------------------------
reset_state()
db = SessionLocal()
try:
    db.query(User).filter(User.id.in_(made_users)).delete(synchronize_session=False)
    db.commit()
    check("test users removed", db.query(User).count() == 3, str(db.query(User).count()))
    check("reservations cleaned up", db.query(GPUReservation).count() == 0, "")
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
    if LOCK_OFF:
        print("(expected: the user-row lock is disabled -- this is the broken build)")
    sys.exit(1)
print("all checks passed")
