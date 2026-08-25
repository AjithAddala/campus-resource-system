"""BENCHMARK 4 — waitlist promotion under concurrent drops.

    docker compose exec app python -m tests.concurrency.benchmark_4_waitlist

Students drop the same full offering SIMULTANEOUSLY while others are
queued. Each drop frees one seat and promotes one queued student in the
SAME transaction, so the seat never becomes briefly available to an
ordinary registration.

--------------------------------------------------------------------
THE SCENARIO IN THE PLAN DOES NOT SEPARATE THE TWO BUILDS
--------------------------------------------------------------------
`EXECUTION_PLAN.md` specifies *"2 concurrent drops on a course with 3
waitlisted students; no offering lock -> same entry promoted twice /
seat lost"*. Measured, the broken build passes it **15/15**:

    2 droppers, 3 queued        promotions   (enrolled_count, rows)
    ---------------------------------------------------------------
    no offering lock            {2: 15}      {(2, 2): 15}    <- PASSES
    + offering lock             {2: 15}      {(2, 2): 15}

Two reasons, and both are worth knowing:

  1. **`SKIP LOCKED` already prevents the double promotion.** Two drops
     racing on the same queue both read the same oldest entry, but the
     first to lock that candidate's user row keeps it and the second is
     SKIPPED onto the next entry. The mechanism item 9 introduced to
     avoid a deadlock also, incidentally, stops the failure this
     benchmark was written to show. The offering lock is not what
     prevents "the same entry promoted twice".
  2. **With one promotion per drop the counter arithmetic nets to
     zero.** Each transaction does `enrolled_count - 1 + 1`, so a lost
     update writes back the same number and the corruption is invisible.

This is the Benchmark 2 finding a second time: a concurrency assertion
that passes against the build it exists to indict. Recorded rather than
quietly re-specified.

--------------------------------------------------------------------
WHAT THE OFFERING LOCK ACTUALLY PROTECTS, AND THE SCENARIO THAT SHOWS IT
--------------------------------------------------------------------
`enrolled_count`, exactly as at Benchmark 1 -- promotion did not change
what that row's lock is for. Make the drops outnumber the queue so the
arithmetic no longer nets to zero, and the builds separate cleanly:

    8 droppers, 3 queued        promotions   (enrolled_count, rows)
    ---------------------------------------------------------------
    no offering lock            {3: 10}      {(7, 3): 10}   <- 10/10 WRONG
    + offering lock             {3: 10}      {(3, 3): 10}

Seven seats recorded as taken against three real enrollments. Every
subsequent registration is refused against a number that is a fiction.
Both scenarios run by default; the first is kept because "the specified
test passes on the broken build" is a result, not a nuisance.

--------------------------------------------------------------------
WHAT IS ACTUALLY UNDER TEST, AND WHAT IS NOT
--------------------------------------------------------------------
The offering row is the serialization point, exactly as it is for
registration at Benchmark 1: it holds `enrolled_count`, so it is the row
that gets locked. Promotion reads the waitlist under that lock, which is
what makes "the oldest entry" a stable answer rather than a snapshot two
transactions can both act on.

`SKIP LOCKED` on the candidate's user row is a DIFFERENT mechanism and
this scenario cannot see it -- the queued students here are idle, so no
candidate row is ever locked and the skip clause never executes. That is
why COLUMN 3 exists; see below.

--------------------------------------------------------------------
BROKEN vs FIXED
--------------------------------------------------------------------
`BENCHMARK_UNSAFE_NO_OFFERING_LOCK` in `.env` drops `FOR UPDATE` from the
offering read in `courses.service.drop` and changes nothing else --
`populate_existing()` stays, so the broken build reads a FRESH
`enrolled_count` and a FRESH waitlist. The bug is not a stale read; it is
a correct read that nothing was holding still.

The same flag also drops the lock from `register`, which Benchmark 1
uses. Nothing here registers concurrently, so the two do not interfere.

--------------------------------------------------------------------
COLUMN 3 -- THE ONE THE PLAN DID NOT ASK FOR
--------------------------------------------------------------------
Added by B's response to outstanding item 9 (DECISIONS.md, session 15).
Item 9's claim is that promotion **never waits** for a candidate's user
row, and the two columns above never lock one, so they would ship that
mechanism unmeasured. This project has recorded that failure three times
-- Benchmark 2 passing against the build it indicts, the Deadline 3 room
checks passing against a stub, and the session-14 room test that stopped
contending the exclusion constraint and would still have passed.

Column 3 holds the first candidate's user row from a second session and
measures whether the drop returns anyway. It is **deterministic** -- a
held lock is not a race -- so it runs once rather than over trials.
"""
import argparse
import functools
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.models import (  # noqa: E402
    Course,
    CourseOffering,
    Enrollment,
    EnrollmentStatus,
    Role,
    User,
    WaitlistEntry,
)
from tests.concurrency.harness import (  # noqa: E402
    Call,
    DBConcurrencyObserver,
    fire,
    tally,
    tool_session_factory,
)

print = functools.partial(print, flush=True)  # noqa: A001

BASE = "http://localhost:8000/api/v1"
TRIALS = 15
# The scenario EXECUTION_PLAN.md specifies. It does not separate the two
# builds -- see the module docstring.
PLAN_DROPPERS = 2
# The one that does. Enough concurrent droppers to make the lost update
# on `enrolled_count` land reliably.
SEPARATING_DROPPERS = 8
QUEUED = 3

Session = tool_session_factory()

made_users: list[int] = []
made_courses: list[int] = []
made_offerings: list[int] = []


def make_users(n: int) -> list[tuple[int, str]]:
    """`n` fresh STUDENTs -> [(id, token)].

    Tokens are minted directly rather than through `POST /auth/login`,
    because Argon2 is deliberately slow and this benchmark needs five
    accounts per scenario, not five HTTP round trips through a hash.
    """
    out = []
    db = Session()
    try:
        pw = hash_password("benchmark-4-password")
        for i in range(n):
            user = User(
                name=f"B4 {i}",
                email=f"b4-{time.time_ns()}-{i}@iitk.ac.in",
                password_hash=pw,
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


def make_offering(capacity: int) -> int:
    db = Session()
    try:
        instructor = db.query(User).filter(User.role == Role.FACULTY).first()
        course = Course(code=f"B4{time.time_ns() % 10**8}", name="Benchmark 4")
        db.add(course)
        db.flush()
        offering = CourseOffering(
            course_id=course.id,
            instructor_id=instructor.id,
            semester="AUTUMN",
            year=2033,
            start_time="07:00",
            end_time="08:00",
            days="U",
            capacity=capacity,
            enrolled_count=0,
        )
        db.add(offering)
        db.commit()
        made_courses.append(course.id)
        made_offerings.append(offering.id)
        return offering.id
    finally:
        db.close()


def reset(offering_id: int) -> None:
    """Back to an empty offering with an empty queue, between trials.

    Enrollments are set to DROPPED rather than deleted: `enrollment_unique`
    is unconditional, so the rows must survive for the next trial's
    register to be an UPDATE -- which is the same path promotion takes.
    """
    db = Session()
    try:
        db.query(WaitlistEntry).filter(
            WaitlistEntry.course_offering_id == offering_id
        ).delete(synchronize_session=False)
        db.query(Enrollment).filter(
            Enrollment.course_offering_id == offering_id
        ).update({"status": EnrollmentStatus.DROPPED}, synchronize_session=False)
        db.query(CourseOffering).filter(CourseOffering.id == offering_id).update(
            {"enrolled_count": 0}, synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


def state(offering_id: int, queue_ids: list[int]) -> dict:
    """Everything the assertions below are made of, read once per trial."""
    db = Session()
    try:
        offering = db.get(CourseOffering, offering_id)
        db.refresh(offering)
        active = (
            db.query(Enrollment)
            .filter(
                Enrollment.course_offering_id == offering_id,
                Enrollment.status == EnrollmentStatus.ACTIVE,
            )
            .count()
        )
        promoted = [
            sid
            for sid in queue_ids
            if db.query(Enrollment)
            .filter(
                Enrollment.student_id == sid,
                Enrollment.course_offering_id == offering_id,
                Enrollment.status == EnrollmentStatus.ACTIVE,
            )
            .count()
            == 1
        ]
        still_queued = [
            e.student_id
            for e in db.query(WaitlistEntry)
            .filter(WaitlistEntry.course_offering_id == offering_id)
            .order_by(WaitlistEntry.created_at, WaitlistEntry.id)
            .all()
        ]
        return {
            "counter": offering.enrolled_count,
            "active": active,
            "promoted": promoted,
            "queued": still_queued,
        }
    finally:
        db.close()


def cleanup() -> None:
    db = Session()
    try:
        db.query(WaitlistEntry).filter(
            WaitlistEntry.course_offering_id.in_(made_offerings)
        ).delete(synchronize_session=False)
        db.query(Enrollment).filter(
            Enrollment.course_offering_id.in_(made_offerings)
        ).delete(synchronize_session=False)
        db.query(CourseOffering).filter(
            CourseOffering.id.in_(made_offerings)
        ).delete(synchronize_session=False)
        db.query(Course).filter(Course.id.in_(made_courses)).delete(
            synchronize_session=False
        )
        db.query(User).filter(User.id.in_(made_users)).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def run(trials: int, droppers: int, queued: int) -> tuple[int, int, int]:
    """One scenario. Returns (bad_promotions, reconcile_failures, order_failures)."""
    locked = not get_settings().BENCHMARK_UNSAFE_NO_OFFERING_LOCK
    build = (
        "offering lock (correct)"
        if locked
        else "NO OFFERING LOCK  <- BENCHMARK_UNSAFE_NO_OFFERING_LOCK=true"
    )

    print()
    print(f"  scenario     : {droppers} simultaneous drops, {queued} queued students")
    print(f"  expected     : {min(droppers, queued)} promotions per trial")
    print(f"  trials       : {trials}")

    offering = make_offering(capacity=droppers)
    holders = make_users(droppers)
    queue = make_users(queued)
    queue_ids = [uid for uid, _ in queue]

    bad_promotions = 0
    reconcile_failures = 0
    order_failures = 0
    promotions_seen: dict[int, int] = {}
    counters_seen: dict[tuple[int, int], int] = {}
    responses: dict = {}
    peak_db = 0

    try:
        for _ in range(trials):
            reset(offering)

            import httpx

            for _, token in holders:
                httpx.post(
                    f"{BASE}/offerings/{offering}/register", headers=bearer(token)
                )
            for _, token in queue:
                httpx.post(
                    f"{BASE}/offerings/{offering}/waitlist", headers=bearer(token)
                )

            calls = [
                Call(
                    "DELETE",
                    f"/offerings/{offering}/drop",
                    headers=bearer(token),
                )
                for _, token in holders
            ]

            with DBConcurrencyObserver(Session) as observer:
                results = fire(calls)
            peak_db = max(peak_db, observer.peak.max_active)

            for key, count in tally(results).items():
                responses[key] = responses.get(key, 0) + count

            after = state(offering, queue_ids)

            n = len(after["promoted"])
            promotions_seen[n] = promotions_seen.get(n, 0) + 1
            counters_seen[(after["counter"], after["active"])] = (
                counters_seen.get((after["counter"], after["active"]), 0) + 1
            )

            # One promotion per freed seat, all distinct -- bounded by how
            # many students are queued. With more droppers than queued
            # students the correct answer is the queue length, not the
            # number of drops.
            if n != min(droppers, queued):
                bad_promotions += 1
            # enrolled_count is derived state; if it disagrees with the
            # rows, a seat was invented or lost.
            if after["counter"] != after["active"]:
                reconcile_failures += 1
            # FIFO: the oldest `droppers` entries are the ones consumed.
            if after["promoted"] != queue_ids[:n] or after["queued"] != queue_ids[n:]:
                order_failures += 1

        print("  " + "-" * 68)
        print(f"  build                  : {build}")
        print(f"  promotions per trial   : {dict(sorted(promotions_seen.items()))}")
        print(f"  (enrolled_count, rows) : {dict(sorted(counters_seen.items()))}")
        print(f"  WRONG PROMOTION COUNT  : {bad_promotions}/{trials}")
        print(f"  COUNTER DISAGREES      : {reconcile_failures}/{trials}")
        print(f"  FIFO ORDER BROKEN      : {order_failures}/{trials}")
        print(f"  responses              : {dict(responses)}")
        print(f"  peak DB concurrency    : {peak_db} (submitted {droppers} per trial)")
        print("  " + "-" * 68)
    finally:
        # NOT cleaned here. `column_three` runs after the last scenario and
        # makes rows of its own, so the single cleanup lives in `main` --
        # cleaning per scenario left that column's users and offering
        # behind, and the next gate to run reported them as leaked.
        pass

    return bad_promotions, reconcile_failures, order_failures


def column_three() -> tuple[bool, str]:
    """Hold a candidate's user row and check the drop returns anyway.

    Deterministic: the row is held or it is not, so this runs once. See
    the module docstring for why it exists at all -- the two columns
    above never lock a candidate row, so `SKIP LOCKED` would otherwise
    ship with no measurement behind it.
    """
    import httpx

    HOLD = 5.0
    print()
    print("  COLUMN 3 — SKIP LOCKED, deterministic, one run")

    offering = make_offering(capacity=1)
    holder_id, holder_token = make_users(1)[0]
    (held_id, held_token), (next_id, next_token) = make_users(2)

    httpx.post(f"{BASE}/offerings/{offering}/register", headers=bearer(holder_token))
    httpx.post(f"{BASE}/offerings/{offering}/waitlist", headers=bearer(held_token))
    httpx.post(f"{BASE}/offerings/{offering}/waitlist", headers=bearer(next_token))

    taken = threading.Event()

    def hold() -> None:
        db = Session()
        try:
            db.execute(
                text("SELECT id FROM users WHERE id = :uid FOR UPDATE"),
                {"uid": held_id},
            )
            taken.set()
            time.sleep(HOLD)
            db.rollback()
        finally:
            db.close()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    taken.wait(timeout=10)

    started = time.perf_counter()
    httpx.delete(f"{BASE}/offerings/{offering}/drop", headers=bearer(holder_token))
    elapsed = time.perf_counter() - started

    after = state(offering, [held_id, next_id])
    skipped = after["queued"] == [held_id]
    promoted_next = after["promoted"] == [next_id]
    did_not_wait = elapsed < HOLD / 2

    print(f"    candidate 1's user row : HELD for {HOLD:.0f}s by another session")
    print(f"    drop returned in       : {elapsed:.2f}s")
    print(f"    still queued           : {after['queued']} (expected [{held_id}])")
    print(f"    promoted               : {after['promoted']} (expected [{next_id}])")

    thread.join(timeout=HOLD + 2)
    ok = skipped and promoted_next and did_not_wait
    return ok, (
        f"skipped={skipped} next_promoted={promoted_next} "
        f"returned_in={elapsed:.2f}s (hold {HOLD:.0f}s)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=TRIALS)
    parser.add_argument(
        "--droppers",
        type=int,
        help="run ONE scenario with this many droppers instead of both",
    )
    parser.add_argument("--queued", type=int, default=QUEUED)
    args = parser.parse_args()

    locked = not get_settings().BENCHMARK_UNSAFE_NO_OFFERING_LOCK
    build = (
        "offering lock (correct)"
        if locked
        else "NO OFFERING LOCK  <- BENCHMARK_UNSAFE_NO_OFFERING_LOCK=true"
    )

    print("=" * 72)
    print("BENCHMARK 4 — waitlist promotion under concurrent drops")
    print("=" * 72)
    print(f"  build        : {build}")

    if args.droppers is not None:
        scenarios = [(args.droppers, args.queued)]
    else:
        scenarios = [(PLAN_DROPPERS, QUEUED), (SEPARATING_DROPPERS, QUEUED)]

    try:
        results = [run(args.trials, d, q) for d, q in scenarios]
        skip_ok, skip_detail = column_three()
    finally:
        # One cleanup, after everything that makes rows -- scenarios and
        # column 3 alike. In a `finally` so a crash mid-run does not leave
        # accounts behind for the next gate to trip over.
        cleanup()

    bad = sum(r[0] for r in results)
    recon = sum(r[1] for r in results)
    order = sum(r[2] for r in results)

    print()
    if locked:
        ok = bad == 0 and recon == 0 and order == 0 and skip_ok
        print(
            "RESULT: PASS — every scenario promoted exactly the queue's worth of "
            "students in FIFO order, the counter reconciled with the rows, and "
            "promotion never waited on a held row"
            if ok
            else f"RESULT: FAIL — promotions {bad}, reconciliation {recon}, "
            f"order {order}, skip-locked {skip_ok}"
        )
        return 0 if ok else 1

    print(
        f"RESULT: broken build recorded — wrong promotion count {bad}, "
        f"COUNTER DISAGREED {recon}, FIFO broken {order}"
    )
    print(f"  column 3 (SKIP LOCKED): {skip_detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
