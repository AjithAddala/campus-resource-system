"""BENCHMARK 1 — capacity under concurrency.

    docker compose exec app python -m tests.concurrency.benchmark_1_capacity

500 students register simultaneously for an offering with 50 seats.
Exactly 50 may succeed, `enrolled_count` must equal the number of ACTIVE
enrollment rows, and no request may 500.

Broken build vs fixed build, selected by `BENCHMARK_UNSAFE_NO_OFFERING_LOCK`
in `.env`:

    broken   the seat read is a plain SELECT -- FRESH, thanks to
             populate_existing(), and simply not held still
    fixed    the same read with FOR UPDATE

That the broken build reads a *fresh* value matters. The bug being shown
is not a stale read; it is a correct read of a number that another
transaction changes between the check and the increment. "Add
populate_existing" does not fix it. Only the lock does.

--------------------------------------------------------------------
WHY THIS RUNS TRIALS
--------------------------------------------------------------------
Deadline 4 learned this the expensive way: Benchmark 2's first run
PASSED against the build it exists to indict, because the corruption
window is sub-millisecond and a single race is a coin flip. A benchmark
that reports one number cannot separate two builds. This one runs T
trials and counts how many oversold.

--------------------------------------------------------------------
WHY IT REPORTS ACHIEVED CONCURRENCY
--------------------------------------------------------------------
Three throttles sit between `asyncio.gather` and a row lock -- the httpx
client (harness raises it), the server's thread pool, and the SQLAlchemy
connection pool (default 15). Asking for 500 does not make 500 arrive.
The observer samples `pg_stat_activity` during each run so the table
reports the contention actually achieved, which is the number an
interviewer should be given.

Tokens are minted directly rather than obtained from /auth/login: 500
argon2 verifies would add ~25s of setup to measure a transaction that has
nothing to do with login, and the minted token is byte-for-byte what
login would have issued.
"""
import argparse
import functools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.config import get_settings  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.models import (  # noqa: E402
    Course,
    CourseOffering,
    Enrollment,
    EnrollmentStatus,
    Role,
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

# NOT the application's SessionLocal. That engine is sized for the
# server (40 + 10), and a benchmark importing it opens a second pool of
# the same size against a Postgres that allows 100 connections total --
# which starves the server and surfaces as `QueuePool timed out` in the
# SERVER's log. Measured: 387 of 500 requests returned 500 that way.
SessionLocal = tool_session_factory()

# Long runs with block-buffered stdout show nothing until they end,
# which makes a hung benchmark indistinguishable from a slow one.
print = functools.partial(print, flush=True)  # noqa: A001

STUDENTS = 500
SEATS = 50
TRIALS = 5
# Requests allowed inside the server at once. Not a tuning knob: a
# connection is held for the whole request (see harness.fire_async), so
# in-flight requests ARE connections, and the pool is 40 + 10 = 50.
# **40 is derived from that budget** -- it is the largest round number
# leaving margin under 50 for the server's own bookkeeping.
#
# Two facts, stated separately because they happen to be the same number
# and A read them as one at the Deadline 6 swap review: uvicorn's anyio
# worker pool is ALSO 40. That is agreement, not derivation. If the
# thread pool were the binding constraint, the argument in
# `harness.fire_async` -- that the ceiling is requests in flight rather
# than worker threads -- would be the thing that is wrong. It is not:
# raise the thread pool and this number does not move, because the
# connections run out first.
IN_FLIGHT = 40

made_users: list[int] = []
made_courses: list[int] = []
made_offerings: list[int] = []


def make_students(n: int) -> list[str]:
    """n STUDENT accounts, returned as bearer tokens.

    One argon2 hash reused for every row: the password is never verified
    on this path, and hashing 500 times would cost ~25 seconds to produce
    500 identical-strength hashes nobody reads.
    """
    db = SessionLocal()
    tokens = []
    try:
        hashed = hash_password("benchmark-password")
        for i in range(n):
            user = User(
                name=f"Bench {i}",
                email=f"bench-{i}-{id(db)}@iitk.ac.in",
                password_hash=hashed,
                role=Role.STUDENT,
            )
            db.add(user)
            db.flush()
            made_users.append(user.id)
            tokens.append(create_access_token(user_id=user.id, role=Role.STUDENT.value))
        db.commit()
    finally:
        db.close()
    return tokens


def make_offering(capacity: int) -> int:
    db = SessionLocal()
    try:
        instructor = db.query(User).filter(User.role == Role.FACULTY).first()
        course = Course(code=f"BM1-{len(made_courses)}-{id(db)}"[:50], name="Benchmark 1")
        db.add(course)
        db.flush()
        offering = CourseOffering(
            course_id=course.id,
            instructor_id=instructor.id,
            semester="AUTUMN",
            year=2032,
            start_time="06:00",
            end_time="07:00",
            days="S",
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


def reset_offering(offering_id: int) -> None:
    """Empty the section between trials, counter included."""
    db = SessionLocal()
    try:
        db.query(Enrollment).filter(
            Enrollment.course_offering_id == offering_id
        ).delete(synchronize_session=False)
        db.get(CourseOffering, offering_id).enrolled_count = 0
        db.commit()
    finally:
        db.close()


def observed(offering_id: int) -> tuple[int, int]:
    """(enrolled_count, ACTIVE rows) — the reconciliation pair."""
    db = SessionLocal()
    try:
        counter = db.get(CourseOffering, offering_id).enrolled_count
        rows = (
            db.query(Enrollment)
            .filter(
                Enrollment.course_offering_id == offering_id,
                Enrollment.status == EnrollmentStatus.ACTIVE,
            )
            .count()
        )
        return counter, rows
    finally:
        db.close()


def cleanup() -> None:
    db = SessionLocal()
    try:
        db.query(Enrollment).filter(
            Enrollment.course_offering_id.in_(made_offerings)
        ).delete(synchronize_session=False)
        db.query(CourseOffering).filter(
            CourseOffering.id.in_(made_offerings)
        ).delete(synchronize_session=False)
        db.query(Course).filter(Course.id.in_(made_courses)).delete(
            synchronize_session=False
        )
        db.query(User).filter(User.id.in_(made_users)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def run(students: int, seats: int, trials: int, in_flight: int) -> int:
    settings = get_settings()
    locked = not settings.BENCHMARK_UNSAFE_NO_OFFERING_LOCK
    build = "FIXED (offering row locked)" if locked else "BROKEN (no offering lock)"

    print(f"BENCHMARK 1 — capacity under concurrency")
    print(f"  build      : {build}")
    print(f"  scenario   : {students} registrations submitted at once, {seats} seats")
    print(f"  in flight  : {in_flight} (connection-per-request ceiling; see harness)")
    print(f"  trials     : {trials}")
    print()

    tokens = make_students(students)
    offering_id = make_offering(seats)
    calls = [
        Call(
            "POST",
            f"/api/v1/offerings/{offering_id}/register",
            headers={"Authorization": f"Bearer {t}"},
        )
        for t in tokens
    ]

    oversold_trials = 0
    mismatched_trials = 0
    rows = []

    for trial in range(1, trials + 1):
        reset_offering(offering_id)

        with DBConcurrencyObserver(SessionLocal) as obs:
            results = fire(
                calls, base_url="http://localhost:8000", max_in_flight=in_flight
            )

        counts = tally(results)
        created = counts.get((201, None), 0)
        full = counts.get((409, "CAPACITY_EXHAUSTED"), 0)
        errors = sum(n for (status, _), n in counts.items() if status is None or status >= 500)
        counter, active = observed(offering_id)

        if active > seats or counter > seats:
            oversold_trials += 1
        if counter != active:
            mismatched_trials += 1

        rows.append(
            {
                "trial": trial,
                "201": created,
                "409": full,
                "err": errors,
                "counter": counter,
                "rows": active,
                "peak_db": obs.peak.max_active,
                "lat": latency(results),
            }
        )
        # The FULL tally, not three buckets. An earlier version reported
        # only 201 / 409-CAPACITY_EXHAUSTED / 5xx and a run came back
        # `201=0 409=0 err=0` for 500 requests -- every response had
        # fallen into a bucket that did not exist. A benchmark that can
        # silently drop 500 responses is not an instrument.
        accounted = created + full + errors
        print(
            f"  trial {trial}: enrolled_count={counter:<4} active_rows={active:<4} "
            f"peak_db_conns={obs.peak.max_active:<3} "
            f"median={rows[-1]['lat'].get('median_ms', 0)}ms"
        )
        print(f"           responses: {dict(counts)}")
        if accounted != len(results):
            print(
                f"           !! {len(results) - accounted} responses outside "
                f"the reported buckets"
            )

    print()
    print("  " + "-" * 68)
    print(f"  build                : {build}")
    print(f"  seats                : {seats}")
    print(f"  OVERSOLD TRIALS      : {oversold_trials}/{trials}")
    print(f"  COUNTER MISMATCHES   : {mismatched_trials}/{trials}")
    print(f"  max 201s in a trial  : {max(r['201'] for r in rows)}")
    print(f"  peak DB concurrency  : {max(r['peak_db'] for r in rows)} "
          f"(submitted {students}, in flight {in_flight})")
    print("  " + "-" * 68)

    cleanup()

    if locked:
        ok = oversold_trials == 0 and mismatched_trials == 0 and all(
            r["201"] == seats and r["err"] == 0 for r in rows
        )
        print()
        print("RESULT:", "PASS — exactly", seats, "in every trial, zero over-allocation"
              if ok else "FAIL")
        return 0 if ok else 1

    print()
    print(f"RESULT: broken build recorded — oversold in {oversold_trials}/{trials} trials")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--students", type=int, default=STUDENTS)
    parser.add_argument("--seats", type=int, default=SEATS)
    parser.add_argument("--trials", type=int, default=TRIALS)
    parser.add_argument("--in-flight", type=int, default=IN_FLIGHT)
    args = parser.parse_args()
    try:
        return run(args.students, args.seats, args.trials, args.in_flight)
    except Exception:
        cleanup()
        raise


if __name__ == "__main__":
    sys.exit(main())
