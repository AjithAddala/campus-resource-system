"""BENCHMARK 3 — exactly-once under concurrent retries.

    docker compose exec app python -m tests.concurrency.benchmark_3_exactly_once

The same request, fired N times simultaneously, in the two modes the API
offers:

    no Idempotency-Key  -> N reservations   (every retry is a new hold)
    one Idempotency-Key -> 1 reservation, and N identical responses

Both columns are honest behaviour, not a broken build and a fixed one.
That is why the `Idempotency-Key` header is **optional** and was settled
that way deliberately at Deadline 5: a caller who has not thought about
retries gets today's semantics, a caller who has gets exactly-once, and
this table is the difference between them. Requiring the header would
delete one of its two columns.

--------------------------------------------------------------------
WHAT IS ACTUALLY UNDER TEST
--------------------------------------------------------------------
Not a lock. The serialization point is the `UNIQUE(key, user_id)` index
on `idempotency_keys`: N simultaneous INSERTs of one pair, one proceeds
and the rest BLOCK on the index entry until it commits, then read the
response it stored. **That works before the row exists**, which is the
window a retry actually arrives in and the reason a row lock could not do
this job -- there is nothing to lock until somebody creates it, and
"somebody creates it" is the race.

Two things make the keyed column pass, and dropping either one is a
silent failure this benchmark catches:

  the key insert and the allocation commit in the SAME transaction
      Split them and a retry either allocates twice (allocation
      committed, key not) or replays a success for work that never
      happened (key committed, allocation not).

  a SAVEPOINT around the claim
      A unique violation aborts the whole Postgres transaction, so the
      read that fetches the stored response cannot run afterwards.
      Measured at Deadline 5: 86 of 120 concurrent retries return 500
      without `begin_nested()`.

Everything sequential passes without either. Sequential retries never
race, which is exactly why this column is fired on a barrier.

--------------------------------------------------------------------
WHY FACULTY, AND WHY N DEFAULTS TO 8
--------------------------------------------------------------------
The racers are FACULTY (GPU quota 10), not students (quota 2). The
unkeyed column must be free to allocate all N times: if the quota gate
refuses retries 3..N, the column reports "2 reservations" for a reason
that has nothing to do with idempotency, and the contrast is measuring
the wrong gate.

The plan says "identical request twice". N is 8 by default because two
requests cannot distinguish "retries double-allocate" from noise, while
watching the row count track N makes the claim unmissable. `--retries 2`
reproduces the plan's literal shape.

--------------------------------------------------------------------
WHY TRIALS
--------------------------------------------------------------------
Same lesson as benchmarks 1 and 2, applied before this test lied rather
than after: the corruption window is sub-millisecond and a single race is
a coin flip. Trials are counted, not asserted once.

Ported onto the shared harness from `scripts/check_idempotency.py`, which
ran the keyed half on threads from Deadline 5 (WORK_LOG session 12,
carried item 5). Its fixtures are its own and are cleaned up on the way
out; tokens are minted rather than logged in, for the reason benchmark 1
records.
"""
import argparse
import functools
import json
import sys
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.security import create_access_token, hash_password  # noqa: E402
from app.models import (  # noqa: E402
    GPUCluster,
    GPUReservation,
    IdempotencyKey,
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

TRIALS = 15
RETRIES = 8

made_users: list[int] = []
made_clusters: list[int] = []


def faculty_gpu_limit() -> int | None:
    db = SessionLocal()
    try:
        row = (
            db.query(RoleQuota)
            .filter(
                RoleQuota.role == Role.FACULTY,
                RoleQuota.resource_type == ResourceType.GPU,
            )
            .one_or_none()
        )
    finally:
        db.close()
    if row is None:
        raise SystemExit(
            "No (FACULTY, GPU) row in role_quotas -- run scripts/seed.py first. "
            "A missing row fails closed as QUOTA_NOT_CONFIGURED and every "
            "request would 409."
        )
    return row.max_units


def make_faculty(n: int) -> list[tuple[int, str]]:
    """n FACULTY accounts as (id, bearer token).

    One account per COLUMN per trial. Sharing an account across the two
    columns would carry the unkeyed column's holds into the keyed one and
    make the keyed result depend on a reset rather than on the index.
    """
    db = SessionLocal()
    out = []
    try:
        hashed = hash_password("benchmark-password")
        for i in range(n):
            user = User(
                name=f"Bench3 {i}",
                email=f"bm3-{uuid.uuid4().hex[:12]}@iitk.ac.in",
                password_hash=hashed,
                role=Role.FACULTY,
            )
            db.add(user)
            db.flush()
            made_users.append(user.id)
            out.append(
                (user.id, create_access_token(user_id=user.id, role=Role.FACULTY.value))
            )
        db.commit()
    finally:
        db.close()
    return out


def make_cluster(units: int) -> int:
    """One cluster with room for every retry to succeed.

    Sized past N deliberately: if capacity refused retries 2..N the
    unkeyed column would report 1 reservation and look exactly like the
    keyed one, and the benchmark would claim a guarantee it never tested.
    """
    db = SessionLocal()
    try:
        cluster = GPUCluster(
            name=f"bm3-{uuid.uuid4().hex[:8]}", gpu_count=units, allocated=0
        )
        db.add(cluster)
        db.flush()
        made_clusters.append(cluster.id)
        db.commit()
        return cluster.id
    finally:
        db.close()


def reset_cluster() -> None:
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


def rows_for(user_id: int) -> tuple[int, int]:
    """(ACTIVE reservation rows, units held) for one user."""
    db = SessionLocal()
    try:
        rows = (
            db.query(GPUReservation)
            .filter(
                GPUReservation.user_id == user_id,
                GPUReservation.status == ReservationStatus.ACTIVE,
            )
            .all()
        )
        return len(rows), sum(r.gpu_count for r in rows)
    finally:
        db.close()


def keys_for(user_id: int) -> int:
    db = SessionLocal()
    try:
        return (
            db.query(IdempotencyKey).filter(IdempotencyKey.user_id == user_id).count()
        )
    finally:
        db.close()


def cleanup() -> None:
    db = SessionLocal()
    try:
        db.query(IdempotencyKey).filter(
            IdempotencyKey.user_id.in_(made_users)
        ).delete(synchronize_session=False)
        db.query(GPUReservation).filter(
            GPUReservation.user_id.in_(made_users)
        ).delete(synchronize_session=False)
        db.flush()
        # Joined-table inheritance: delete the mapped object so the ORM
        # removes the `resources` row as well as the `gpu_clusters` one.
        for cluster in db.query(GPUCluster).filter(GPUCluster.id.in_(made_clusters)):
            db.delete(cluster)
        db.query(User).filter(User.id.in_(made_users)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def distinct_bodies(results) -> int:
    """How many DISTINCT 201 bodies came back.

    **1 is the keyed column's other promise.** "Exactly one reservation"
    is only half of exactly-once; the other half is that every retry gets
    the SAME answer, including the same reservation id and created_at. A
    caller that branches on the response would otherwise take a different
    path on the retry, which is the bug idempotency exists to remove.

    Serialized with sorted keys because dicts are unhashable and key order
    is not a difference worth reporting.
    """
    return len(
        {
            json.dumps(r.body, sort_keys=True)
            for r in results
            if r.status == 201 and r.body is not None
        }
    )


def run_column(
    cluster_id: int,
    user_id: int,
    token: str,
    retries: int,
    key: str | None,
) -> dict:
    """Fire `retries` identical requests at once; report what landed.

    The two columns differ in ONE header and nothing else -- same user,
    same cluster, same body, same barrier -- so any difference in the row
    count is attributable to the key and to nothing else.
    """
    headers = {"Authorization": f"Bearer {token}"}
    if key is not None:
        headers["Idempotency-Key"] = key

    calls = [
        Call(
            "POST",
            f"/api/v1/gpus/{cluster_id}/reservations",
            headers=headers,
            json={"gpu_count": 1},
        )
        for _ in range(retries)
    ]

    with DBConcurrencyObserver(SessionLocal) as obs:
        results = fire(calls, base_url="http://localhost:8000")

    rows, held = rows_for(user_id)
    counts = tally(results)
    return {
        "counts": counts,
        "rows": rows,
        "held": held,
        "keys": keys_for(user_id),
        "peak_db": obs.peak.max_active,
        "median_ms": latency(results).get("median_ms", 0),
        "errors": sum(
            n for (status, _), n in counts.items() if status is None or status >= 500
        ),
        "created": counts.get((201, None), 0),
        "distinct_bodies": distinct_bodies(results),
    }


def run(trials: int, retries: int) -> int:
    limit = faculty_gpu_limit()
    if limit is not None and retries > limit:
        raise SystemExit(
            f"--retries {retries} exceeds the FACULTY GPU quota of {limit}. The "
            f"unkeyed column would be truncated by the quota gate, which is a "
            f"different guarantee than the one this benchmark measures. Use "
            f"--retries {limit} or lower."
        )

    print("BENCHMARK 3 — exactly-once under concurrent retries")
    print(f"  scenario   : the SAME request fired {retries} times at once,")
    print("               with and without an Idempotency-Key")
    print(f"  racers     : FACULTY (GPU quota {limit}) so the unkeyed column")
    print("               is never truncated by the quota gate")
    print(f"  trials     : {trials}")
    print()

    # Two accounts per trial: one for each column. See make_faculty.
    accounts = make_faculty(trials * 2)
    cluster_id = make_cluster(max(retries * 2, 8))

    unkeyed_rows: Counter = Counter()
    keyed_rows: Counter = Counter()
    unkeyed_status: Counter = Counter()
    keyed_status: Counter = Counter()
    divergent_bodies = 0
    keyed_key_rows: Counter = Counter()
    errors_total = 0
    peak_db = 0

    for trial in range(1, trials + 1):
        no_key_user, no_key_token = accounts[(trial - 1) * 2]
        keyed_user, keyed_token = accounts[(trial - 1) * 2 + 1]

        reset_cluster()
        a = run_column(cluster_id, no_key_user, no_key_token, retries, key=None)

        reset_cluster()
        key = f"bm3-{uuid.uuid4().hex}"
        b = run_column(cluster_id, keyed_user, keyed_token, retries, key=key)

        unkeyed_rows[a["rows"]] += 1
        keyed_rows[b["rows"]] += 1
        # SUMMED per status, not built as a dict comprehension keyed on
        # status: two different codes can share one status -- 409 is
        # QUOTA_EXCEEDED, CAPACITY_EXHAUSTED and IDEMPOTENCY_IN_PROGRESS --
        # and `{status: n for (status, code), n in ...}` keeps whichever
        # came last, silently undercounting the rest. That is the same
        # class of bug as benchmark 1's vanished response buckets, in the
        # line that exists to report them.
        for (status, _), n in a["counts"].items():
            unkeyed_status[status] += n
        for (status, _), n in b["counts"].items():
            keyed_status[status] += n
        keyed_key_rows[b["keys"]] += 1
        errors_total += a["errors"] + b["errors"]
        peak_db = max(peak_db, a["peak_db"], b["peak_db"])
        # >1 means two retries were told different things about the same
        # request. 0 means nothing was created at all, which is not
        # "identical" either -- both are failures of the keyed column.
        if b["distinct_bodies"] != 1:
            divergent_bodies += 1

        print(
            f"  trial {trial:<3}: no key -> rows={a['rows']:<3} 201={a['created']:<3} "
            f"| key -> rows={b['rows']:<3} 201={b['created']:<3} "
            f"bodies={b['distinct_bodies']} key_rows={b['keys']:<2} "
            f"peak_db={max(a['peak_db'], b['peak_db']):<3} median={b['median_ms']}ms"
        )
        # Anything outside 201 and 5xx is printed in full rather than
        # bucketed. Benchmark 1 once reported `201=0 409=0 err=0` for 500
        # requests because every response had landed in a bucket that did
        # not exist; a benchmark that can silently drop responses is not an
        # instrument.
        for label, col in (("no key", a), ("key", b)):
            other = {
                k: v
                for k, v in col["counts"].items()
                if k[0] != 201 and (k[0] is None or k[0] < 500)
            }
            if other:
                print(f"           {label} other responses: {other}")

    print()
    print("  " + "-" * 68)
    print(f"  retries per trial      : {retries}")
    print(f"  NO KEY   rows per trial: {dict(sorted(unkeyed_rows.items()))} "
          f"  (one hold per retry)")
    print(f"  WITH KEY rows per trial: {dict(sorted(keyed_rows.items()))} "
          f"  (must be 1)")
    print(f"  NO KEY   statuses      : {dict(sorted(unkeyed_status.items()))}")
    print(f"  WITH KEY statuses      : {dict(sorted(keyed_status.items()))}")
    print(f"  idempotency_keys rows  : {dict(sorted(keyed_key_rows.items()))} "
          f"  (must be 1)")
    print(f"  DIVERGENT-BODY TRIALS  : {divergent_bodies}/{trials} "
          f"  (keyed replays must be identical)")
    print(f"  5xx / transport        : {errors_total}")
    print(f"  peak DB concurrency    : {peak_db} (submitted {retries} per column)")
    print("  " + "-" * 68)

    # The keyed column's promise, and the contrast that gives it meaning.
    keyed_ok = (
        set(keyed_rows) == {1}
        and set(keyed_key_rows) == {1}
        and divergent_bodies == 0
        and 500 not in keyed_status
        and 422 not in keyed_status  # IDEMPOTENCY_KEY_REUSED: bodies are identical
        and 409 not in keyed_status  # IDEMPOTENCY_IN_PROGRESS should be unreachable
    )
    contrast_ok = set(unkeyed_rows) == {retries}

    print()
    if keyed_ok and contrast_ok:
        print(
            f"RESULT: PASS — {retries} retries produced {retries} holds unkeyed "
            f"and exactly 1 keyed, with identical bodies, in every trial"
        )
        cleanup()
        return 0

    if not contrast_ok:
        print(
            f"RESULT: FAIL — the unkeyed column did not allocate {retries} times "
            f"({dict(sorted(unkeyed_rows.items()))}). Some other gate refused "
            f"those retries, so the keyed column proves nothing by comparison."
        )
    if not keyed_ok:
        print(
            f"RESULT: FAIL — keyed retries did not land on exactly one "
            f"reservation ({dict(sorted(keyed_rows.items()))}), one key row "
            f"({dict(sorted(keyed_key_rows.items()))}), and one response body "
            f"({divergent_bodies}/{trials} trials diverged)."
        )
    cleanup()
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=TRIALS)
    parser.add_argument(
        "--retries",
        type=int,
        default=RETRIES,
        help="identical requests fired at once, per column (default 8)",
    )
    args = parser.parse_args()
    try:
        return run(args.trials, args.retries)
    except SystemExit:
        raise
    except Exception:
        cleanup()
        raise


if __name__ == "__main__":
    sys.exit(main())
