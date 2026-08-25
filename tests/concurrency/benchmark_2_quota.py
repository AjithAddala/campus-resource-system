"""BENCHMARK 2 — quota under concurrency. THE argument of this project.

    docker compose exec app python -m tests.concurrency.benchmark_2_quota

ONE student, quota 2, fires two 2-unit requests SIMULTANEOUSLY at two
DIFFERENT clusters.

    resource lock only  -> both succeed, held = 4
    + user-row lock     -> exactly one succeeds, held = 2

--------------------------------------------------------------------
WHY THE CLUSTERS MUST DIFFER
--------------------------------------------------------------------
This is the entire point and it is easy to lose. Both requests are
*correct as far as any resource is concerned*: two different cluster
rows, so `FOR UPDATE` on each never contends, each cluster has room, and
neither is overbooked. Every capacity check passes honestly. The
invariant that breaks is a fact about the **user** -- "a user never holds
more than their role permits" -- and no lock on a resource row can see
it, because the two allocations touch no common resource row.

So the fix is not "add a lock". The resource lock was already there and
was already right. It was the **wrong lock for that invariant**, and the
right one is the user row, taken before the resource row and held to
commit. Both of us must be able to say that unprompted (EXECUTION_PLAN.md,
Deadline 6).

Point the two racers at the SAME cluster and this benchmark measures
nothing: the cluster lock serializes them, the second re-reads held
units, and the broken build passes.

--------------------------------------------------------------------
BROKEN vs FIXED
--------------------------------------------------------------------
Selected by `BENCHMARK_UNSAFE_NO_USER_LOCK` in `.env`, which drops ONE
line from `gpus/service.py` -- the `SELECT User.id ... FOR UPDATE` -- and
changes nothing else. The quota arithmetic under it is the same
arithmetic. That is deliberate: the bug is not a miscalculation, it is a
correct calculation on a value nothing was holding still.

--------------------------------------------------------------------
WHY TRIALS, NOT AN ASSERTION
--------------------------------------------------------------------
The first ever run of this race against the UNLOCKED build reported
held = 2 -- it PASSED, against the build it exists to indict. The window
between "SUM held units" and COMMIT is well under a millisecond and two
HTTP requests released on a barrier do not reliably land inside it. One
run is a coin flip; a benchmark that reports one number cannot separate
two builds. This one runs T trials and counts how many ended over quota,
which is a number that separates them.

--------------------------------------------------------------------
WHAT IS NEW HERE VERSUS check_gpus.py
--------------------------------------------------------------------
The same race has run inside `scripts/check_gpus.py` since Deadline 4, on
threads. Nothing was wrong with it. This is the port onto the shared
harness asked for at Deadline 6 (WORK_LOG session 12, carried item 5):
one asyncio barrier for every benchmark, achieved concurrency sampled
from `pg_stat_activity` rather than assumed, and the full response tally
printed rather than three hand-picked buckets. Four benchmarks in one
style is a better answer to Deadline 9's stranger than four scripts in
two styles.

Its fixtures are its own -- its own clusters, its own students, tokens
minted directly rather than obtained from /auth/login (see benchmark 1
for why) -- so it runs against a seeded database without disturbing the
seed, and cleans up on the way out. It reads the STUDENT/GPU limit from
`role_quotas` rather than hardcoding 2, because that row is
admin-editable policy and a benchmark that hardcodes policy silently
stops testing the system the day the policy changes.
"""
import argparse
import functools
import sys
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.config import get_settings  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.models import (  # noqa: E402
    GPUCluster,
    GPUReservation,
    ReservationStatus,
    ResourceType,
    Role,
    RoleQuota,
    User,
)
from tests.concurrency.harness import (  # noqa: E402
    Call,
    DBConcurrencyObserver,
    fire,
    latency,
    tally,
    tool_session_factory,
)

SessionLocal = tool_session_factory()

print = functools.partial(print, flush=True)  # noqa: A001

TRIALS = 25
RACERS = 2

made_users: list[int] = []
made_clusters: list[int] = []


def student_gpu_limit() -> int:
    """The STUDENT/GPU cap, read from policy rather than hardcoded."""
    db = SessionLocal()
    try:
        row = (
            db.query(RoleQuota)
            .filter(
                RoleQuota.role == Role.STUDENT,
                RoleQuota.resource_type == ResourceType.GPU,
            )
            .one_or_none()
        )
    finally:
        db.close()
    if row is None:
        raise SystemExit(
            "No (STUDENT, GPU) row in role_quotas -- run scripts/seed.py first. "
            "A missing row fails closed as QUOTA_NOT_CONFIGURED, so every "
            "request would 409 and the benchmark would measure nothing."
        )
    if row.max_units is None:
        raise SystemExit(
            "(STUDENT, GPU) quota is NULL (unlimited). This benchmark needs a "
            "finite cap to race against."
        )
    return row.max_units


def make_students(n: int) -> list[tuple[int, str]]:
    """n STUDENT accounts as (id, bearer token).

    A fresh student PER TRIAL rather than one reused account: held units
    are the measured quantity, and reusing an account makes every trial
    depend on the cleanup of the one before it. One argon2 hash for all of
    them -- the password is never verified on this path.
    """
    db = SessionLocal()
    out = []
    try:
        hashed = hash_password("benchmark-password")
        for i in range(n):
            user = User(
                name=f"Bench2 {i}",
                email=f"bm2-{uuid.uuid4().hex[:12]}@iitk.ac.in",
                password_hash=hashed,
                role=Role.STUDENT,
            )
            db.add(user)
            db.flush()
            made_users.append(user.id)
            out.append(
                (user.id, create_access_token(user_id=user.id, role=Role.STUDENT.value))
            )
        db.commit()
    finally:
        db.close()
    return out


def make_clusters(n: int, units_each: int) -> list[int]:
    """n clusters, each with room for the whole request several times over.

    Sized so capacity can never be the gate that refuses anyone. If a racer
    is refused here it must be the quota gate that refused it, or the
    result says nothing about quota.
    """
    db = SessionLocal()
    try:
        ids = []
        for i in range(n):
            cluster = GPUCluster(
                name=f"bm2-{uuid.uuid4().hex[:8]}-{i}",
                gpu_count=units_each * 4,
                allocated=0,
            )
            db.add(cluster)
            db.flush()
            ids.append(cluster.id)
            made_clusters.append(cluster.id)
        db.commit()
        return ids
    finally:
        db.close()


def reset_clusters() -> None:
    """Drop this benchmark's holds and zero its counters between trials."""
    db = SessionLocal()
    try:
        db.query(GPUReservation).filter(
            GPUReservation.gpu_cluster_id.in_(made_clusters)
        ).delete(synchronize_session=False)
        for cluster in db.query(GPUCluster).filter(GPUCluster.id.in_(made_clusters)):
            cluster.allocated = 0
        db.commit()
    finally:
        db.close()


def held_by(user_id: int) -> int:
    """Units held, computed exactly as the quota gate computes them."""
    db = SessionLocal()
    try:
        return sum(
            r.gpu_count
            for r in db.query(GPUReservation).filter(
                GPUReservation.user_id == user_id,
                GPUReservation.status == ReservationStatus.ACTIVE,
            )
        )
    finally:
        db.close()


def cleanup() -> None:
    db = SessionLocal()
    try:
        db.query(GPUReservation).filter(
            GPUReservation.user_id.in_(made_users)
        ).delete(synchronize_session=False)
        db.flush()
        for cluster in db.query(GPUCluster).filter(GPUCluster.id.in_(made_clusters)):
            db.delete(cluster)
        db.query(User).filter(User.id.in_(made_users)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def run(trials: int, racers: int) -> int:
    locked = not get_settings().BENCHMARK_UNSAFE_NO_USER_LOCK
    build = "FIXED (user row locked)" if locked else "BROKEN (no user lock)"
    limit = student_gpu_limit()

    print("BENCHMARK 2 — quota under concurrency")
    print(f"  build      : {build}")
    print(f"  scenario   : 1 student, {racers} x {limit}-unit requests fired at")
    print(f"               {racers} DIFFERENT clusters at once")
    print(f"  quota      : STUDENT/GPU = {limit} units (read from role_quotas)")
    print(f"  trials     : {trials}")
    print()

    students = make_students(trials)
    cluster_ids = make_clusters(racers, limit)

    over_quota_trials = 0
    held_seen: Counter = Counter()
    successes_seen: Counter = Counter()
    peak_db = 0
    error_total = 0

    for trial, (user_id, token) in enumerate(students, start=1):
        reset_clusters()
        calls = [
            Call(
                "POST",
                f"/api/v1/gpus/{cid}/reservations",
                headers={"Authorization": f"Bearer {token}"},
                json={"gpu_count": limit},
            )
            for cid in cluster_ids
        ]

        with DBConcurrencyObserver(SessionLocal) as obs:
            results = fire(calls, base_url="http://localhost:8000")

        counts = tally(results)
        created = counts.get((201, None), 0)
        refused = counts.get((409, "QUOTA_EXCEEDED"), 0)
        errors = sum(
            n for (status, _), n in counts.items() if status is None or status >= 500
        )
        held = held_by(user_id)

        held_seen[held] += 1
        successes_seen[created] += 1
        error_total += errors
        peak_db = max(peak_db, obs.peak.max_active)
        if held > limit:
            over_quota_trials += 1

        print(
            f"  trial {trial:<3}: held={held:<3} 201={created} "
            f"409-QUOTA={refused} peak_db_conns={obs.peak.max_active:<3} "
            f"median={latency(results).get('median_ms', 0)}ms"
        )
        accounted = created + refused + errors
        if accounted != len(results):
            print(f"           responses: {dict(counts)}")
            print(
                f"           !! {len(results) - accounted} responses outside "
                f"the reported buckets"
            )

    print()
    print("  " + "-" * 68)
    print(f"  build                : {build}")
    print(f"  quota limit          : {limit} units")
    print(f"  held units observed  : {dict(sorted(held_seen.items()))}")
    print(f"  successes per trial  : {dict(sorted(successes_seen.items()))}")
    print(f"  OVER-QUOTA TRIALS    : {over_quota_trials}/{trials}")
    print(f"  5xx / transport      : {error_total}")
    print(f"  peak DB concurrency  : {peak_db} (submitted {racers} per trial)")
    print("  " + "-" * 68)

    cleanup()

    if locked:
        ok = (
            over_quota_trials == 0
            and set(held_seen) <= {limit}
            and set(successes_seen) == {1}
            and error_total == 0
        )
        print()
        print(
            f"RESULT: PASS — exactly one success and held == {limit} in every trial"
            if ok
            else "RESULT: FAIL"
        )
        return 0 if ok else 1

    print()
    print(
        f"RESULT: broken build recorded — over quota in "
        f"{over_quota_trials}/{trials} trials"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=TRIALS)
    parser.add_argument(
        "--racers",
        type=int,
        default=RACERS,
        help="concurrent requests per trial, one cluster each (default 2)",
    )
    args = parser.parse_args()
    try:
        return run(args.trials, args.racers)
    except Exception:
        cleanup()
        raise


if __name__ == "__main__":
    sys.exit(main())
