"""Deadline 7's gate, written BEFORE the promotion transaction exists.

    docker compose exec app python scripts/check_waitlist.py

Seventh gate. Two halves, and the split is the point:

  PART 1  facts the promotion transaction will REST ON, all true today.
          These run for real and must pass now.
  PART 2  the promotion assertions themselves. Written out, and skipped
          while the waitlist endpoints do not exist. The moment they do,
          these run without anyone editing this file.

--------------------------------------------------------------------
WHY THE GATE IS WRITTEN FIRST
--------------------------------------------------------------------
This project has been bitten twice by tests written after the code:
Benchmark 2 passed against the very build it existed to indict, and
Deadline 3's room checks passed while `dependencies.py` was still a stub
that accepted any token and no token alike. A test written afterwards
tends to test the code that was written, not the claim that was made.

The Deadline 7 checkpoint is already specific -- *"promotion follows
FIFO, respects quota, and never double-promotes under concurrent
drops"* -- so the assertions are knowable now, and none of them depends
on how outstanding items 9, 10 or 7 are ratified. Item 9 decides HOW the
transaction takes its locks; it does not change what must be true
afterwards.

--------------------------------------------------------------------
WHAT PART 1 IS ACTUALLY FOR
--------------------------------------------------------------------
It is not filler. `ORDER BY created_at, id` is the entire FIFO
guarantee, and the `id` tiebreak is load-bearing for a reason that is
easy to state and easy to disbelieve: `func.now()` is **transaction
start time**, so every waitlist row written in one transaction shares a
`created_at` to the microsecond. Part 1 proves that on the live database
rather than trusting the docstring -- and if it ever stops being true,
the promotion transaction's ordering is undefined and nobody would
notice from the promotion tests alone.
"""
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

# Running a file inside scripts/ puts scripts/ on sys.path, not the repo
# root, so `app` would not import. Same job as alembic.ini's prepend_sys_path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, select, text  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Course,
    CourseOffering,
    Enrollment,
    EnrollmentStatus,
    Role,
    User,
    WaitlistEntry,
)

BASE = "http://localhost:8000/api/v1"
SEED_PASSWORD = "campus123"
TEST_PASSWORD = "check-password-123"

failures: list[str] = []
pending: list[str] = []
made_users: list[int] = []
made_courses: list[int] = []
made_offerings: list[int] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{'  -> ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def skip(label: str, why: str) -> None:
    print(f"SKIP  {label}  -> {why}")
    pending.append(label)


def login(email: str, password: str = SEED_PASSWORD) -> str:
    r = httpx.post(
        f"{BASE}/auth/login", data={"username": email, "password": password}
    )
    r.raise_for_status()
    return r.json()["access_token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def make_student() -> tuple[int, str]:
    """One fresh STUDENT -> (user_id, bearer token)."""
    db = SessionLocal()
    try:
        email = f"wl-{uuid.uuid4().hex[:12]}@iitk.ac.in"
        user = User(
            name="Waitlist Check",
            email=email,
            password_hash=hash_password(TEST_PASSWORD),
            role=Role.STUDENT,
        )
        db.add(user)
        db.flush()
        uid = user.id
        made_users.append(uid)
        db.commit()
    finally:
        db.close()
    return uid, login(email, TEST_PASSWORD)


def make_offering(capacity: int, days: str = "MWF", start="09:00", end="10:00") -> int:
    db = SessionLocal()
    try:
        instructor = db.query(User).filter(User.role == Role.FACULTY).first()
        course = Course(code=f"WL{uuid.uuid4().hex[:6].upper()}", name="Waitlist Check")
        db.add(course)
        db.flush()
        offering = CourseOffering(
            course_id=course.id,
            instructor_id=instructor.id,
            semester="AUTUMN",
            year=2032,
            start_time=start,
            end_time=end,
            days=days,
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


def waitlist_endpoints_exist() -> bool:
    """Does B's Deadline 7 column exist yet?

    Read from the live OpenAPI document rather than from a guess about
    the path, because outstanding item 10 has not been ratified and the
    route may land as `POST /offerings/{id}/waitlist` or as a fall-through
    on register. Any route mentioning `waitlist` counts.
    """
    try:
        spec = httpx.get("http://localhost:8000/openapi.json", timeout=10).json()
    except Exception:  # noqa: BLE001
        return False
    return any("waitlist" in path for path in spec.get("paths", {}))


# ===========================================================================
# PART 1 — the ground the promotion transaction stands on
# ===========================================================================

print("=" * 70)
print("PART 1  facts promotion will rest on -- these must pass TODAY")
print("=" * 70)

db = SessionLocal()
try:
    insp = inspect(db.get_bind())
    columns = {c["name"] for c in insp.get_columns("waitlist_entries")}
    indexes = {i["name"] for i in insp.get_indexes("waitlist_entries")}
    uniques = {u["name"] for u in insp.get_unique_constraints("waitlist_entries")}
finally:
    db.close()

check("waitlist_entries table exists", bool(columns), str(sorted(columns)))

# No stored position. `c86676652ca2` dropped it, and DECISIONS.md records
# why: renumbering after a promotion (`SET position = position - 1`)
# transiently collides, because Postgres checks unique constraints per
# row during an UPDATE. Dropping the column deleted the problem instead
# of constraining it -- so a `position` column reappearing is a
# regression, not a feature.
check(
    "there is NO stored position column",
    "position" not in columns,
    "dropped in c86676652ca2; position is ROW_NUMBER() at read time",
)

check(
    "UNIQUE(student_id, course_offering_id) exists",
    "waitlist_unique" in uniques,
    str(sorted(uniques)),
)
check(
    "the promotion index exists (offering, created_at)",
    "ix_waitlist_entries_offering_created" in indexes,
    str(sorted(indexes)),
)

# --- the created_at trap, proven rather than quoted ---------------------
#
# THE assertion in Part 1. `func.now()` is TRANSACTION START TIME in
# Postgres, so rows inserted in one transaction share a created_at
# exactly. If that is true, `ORDER BY created_at` alone cannot express
# FIFO and the `id` tiebreak is the whole guarantee.
offering_id = make_offering(capacity=1)
trap_students = [make_student()[0] for _ in range(3)]

db = SessionLocal()
try:
    # All three in ONE transaction, deliberately -- this is the case the
    # seed script was warned about at Deadline 2 and the case promotion
    # will actually meet, because B's join endpoint commits per request
    # but a benchmark seeding a queue may not.
    for uid in trap_students:
        db.add(WaitlistEntry(student_id=uid, course_offering_id=offering_id))
    db.commit()

    rows = db.execute(
        select(WaitlistEntry)
        .where(WaitlistEntry.course_offering_id == offering_id)
        .order_by(WaitlistEntry.created_at, WaitlistEntry.id)
    ).scalars().all()

    stamps = {r.created_at for r in rows}
    check(
        "rows written in ONE transaction share created_at exactly",
        len(rows) == 3 and len(stamps) == 1,
        f"{len(rows)} rows, {len(stamps)} distinct created_at",
    )
    check(
        "so ORDER BY created_at alone CANNOT express FIFO",
        len(stamps) == 1,
        "the id tiebreak is load-bearing, not decorative",
    )
    check(
        "ORDER BY created_at, id returns insertion order",
        [r.student_id for r in rows] == trap_students,
        f"{[r.student_id for r in rows]} == {trap_students}",
    )
    check(
        "ids are strictly increasing (the tiebreak is total)",
        all(a.id < b.id for a, b in zip(rows, rows[1:])),
        str([r.id for r in rows]),
    )
finally:
    db.close()

# --- position is a display value, computed at read time -----------------
db = SessionLocal()
try:
    ranked = db.execute(
        text(
            "SELECT student_id, ROW_NUMBER() OVER (ORDER BY created_at, id) AS pos "
            "FROM waitlist_entries WHERE course_offering_id = :oid"
        ),
        {"oid": offering_id},
    ).all()
    check(
        "ROW_NUMBER() reproduces FIFO position without storing it",
        [r.student_id for r in ranked] == trap_students
        and [r.pos for r in ranked] == [1, 2, 3],
        str([(r.student_id, r.pos) for r in ranked]),
    )
finally:
    db.close()

# --- the unconditional-unique trap that promotion will hit --------------
#
# `enrollment_unique` has no `WHERE status = 'ACTIVE'` clause, so a
# student who dropped STILL OWNS a row. Promotion must therefore UPDATE,
# not INSERT -- the same trap course registration hit at Deadline 4, and
# the docstring in courses/service.py says promotion is the next place it
# bites. Proven here so promotion is written knowing it.
solo_id, solo_token = make_student()
solo_offering = make_offering(capacity=1, start="14:00", end="15:00", days="T")

r = httpx.post(
    f"{BASE}/offerings/{solo_offering}/register", headers=bearer(solo_token), timeout=30
)
check("setup: student registers", r.status_code == 201, str(r.status_code))
r = httpx.delete(
    f"{BASE}/offerings/{solo_offering}/drop", headers=bearer(solo_token), timeout=30
)
check("setup: student drops", r.status_code == 200, str(r.status_code))

db = SessionLocal()
try:
    row = db.execute(
        select(Enrollment).where(
            Enrollment.student_id == solo_id,
            Enrollment.course_offering_id == solo_offering,
        )
    ).scalar_one_or_none()
    check(
        "a DROPPED student still owns an enrollment row",
        row is not None and row.status is EnrollmentStatus.DROPPED,
        "promotion must UPDATE this row, never INSERT alongside it",
    )
finally:
    db.close()

# --- registering for a full offering does NOT auto-waitlist (today) -----
#
# This is outstanding item 10 observed rather than decided. Recorded so
# that whichever way item 10 is ratified, the change is visible here
# instead of silently altering what POST /register means.
full_offering = make_offering(capacity=1, start="16:00", end="17:00", days="W")
_, first_token = make_student()
_, second_token = make_student()

httpx.post(
    f"{BASE}/offerings/{full_offering}/register",
    headers=bearer(first_token),
    timeout=30,
)
r = httpx.post(
    f"{BASE}/offerings/{full_offering}/register",
    headers=bearer(second_token),
    timeout=30,
)
detail = r.json().get("detail")
code = detail.get("code") if isinstance(detail, dict) else None
check(
    "full offering -> 409 CAPACITY_EXHAUSTED, not a silent waitlist",
    r.status_code == 409 and code == "CAPACITY_EXHAUSTED",
    f"{r.status_code} {code}  <- item 10 is observed here, not decided",
)

db = SessionLocal()
try:
    n = db.query(WaitlistEntry).filter(
        WaitlistEntry.course_offering_id == full_offering
    ).count()
    check("the refused registration created NO waitlist row", n == 0, f"{n} rows")
finally:
    db.close()


# ===========================================================================
# PART 2 — the promotion assertions
# ===========================================================================

print()
print("=" * 70)
print("PART 2  promotion -- Deadline 7's checkpoint")
print("=" * 70)

if not waitlist_endpoints_exist():
    why = "no /waitlist route in openapi.json -- Deadline 7 not built yet"
    skip("promotion follows FIFO by (created_at, id)", why)
    skip("promotion respects the promoted student's course-load quota", why)
    skip("a quota-breaching candidate is SKIPPED, next eligible promoted", why)
    skip("2 concurrent drops -> exactly 2 DISTINCT promotions", why)
    skip("2 concurrent drops -> no entry promoted twice", why)
    skip("promotion DELETES the waitlist row, renumbering nothing", why)
    skip("promoted student's enrollment is an UPDATE of the DROPPED row", why)
    skip("GET waitlist reports position from ROW_NUMBER(), never stored", why)
else:
    # ---------------------------------------------------------------
    # B's endpoints exist (Deadline 7, session 16). Everything B owns is
    # asserted for real from here; the promotion assertions are A's and
    # are handled at the bottom of this block.
    # ---------------------------------------------------------------
    full = make_offering(capacity=1)
    holder_id, holder_token = make_student()
    first_id, first_token = make_student()
    second_id, second_token = make_student()

    # Fill the single seat, so the offering is genuinely full rather than
    # merely small. Every join below depends on this having worked.
    r = httpx.post(f"{BASE}/offerings/{full}/register", headers=bearer(holder_token))
    check("setup: the seat is taken", r.status_code == 201, str(r.status_code))

    r = httpx.post(f"{BASE}/offerings/{full}/waitlist", headers=bearer(first_token))
    check(
        "join a full offering -> 201 with position 1",
        r.status_code == 201 and r.json()["position"] == 1,
        f"{r.status_code} {r.json()}",
    )

    r = httpx.post(f"{BASE}/offerings/{full}/waitlist", headers=bearer(second_token))
    check(
        "second joiner -> position 2",
        r.status_code == 201 and r.json()["position"] == 2,
        f"{r.status_code} {r.json()}",
    )
    second_entry_id = r.json()["id"]
    second_created = r.json()["created_at"]

    r = httpx.get(f"{BASE}/offerings/{full}/waitlist", headers=bearer(holder_token))
    rows = r.json()
    check(
        "GET waitlist -> both entries, oldest first, positions 1 and 2",
        r.status_code == 200
        and [e["position"] for e in rows] == [1, 2]
        and [e["student_id"] for e in rows] == [first_id, second_id],
        f"{[(e['student_id'], e['position']) for e in rows]}",
    )

    r = httpx.post(f"{BASE}/offerings/{full}/waitlist", headers=bearer(first_token))
    check(
        "joining twice -> 409 ALREADY_WAITLISTED",
        r.status_code == 409 and r.json()["detail"]["code"] == "ALREADY_WAITLISTED",
        f"{r.status_code} {r.json()}",
    )

    r = httpx.post(f"{BASE}/offerings/{full}/waitlist", headers=bearer(holder_token))
    check(
        "the student holding the seat -> 409 ALREADY_ENROLLED",
        r.status_code == 409 and r.json()["detail"]["code"] == "ALREADY_ENROLLED",
        f"{r.status_code} {r.json()}",
    )

    # Item 10's other half: a queue for an available seat is not a queue.
    roomy = make_offering(capacity=5, days="TR", start="14:00", end="15:00")
    r = httpx.post(f"{BASE}/offerings/{roomy}/waitlist", headers=bearer(first_token))
    check(
        "joining a NOT-full offering -> 409 OFFERING_NOT_FULL",
        r.status_code == 409 and r.json()["detail"]["code"] == "OFFERING_NOT_FULL",
        f"{r.status_code} {r.json()}",
    )

    faculty_email = None
    db = SessionLocal()
    try:
        faculty_email = db.query(User).filter(User.role == Role.FACULTY).first().email
    finally:
        db.close()
    r = httpx.post(
        f"{BASE}/offerings/{full}/waitlist", headers=bearer(login(faculty_email))
    )
    check(
        "FACULTY token on join -> 403, same gate as register",
        r.status_code == 403,
        str(r.status_code),
    )

    r = httpx.get(f"{BASE}/offerings/999999/waitlist", headers=bearer(holder_token))
    check(
        "GET waitlist on a nonexistent offering -> 404, not []",
        r.status_code == 404,
        str(r.status_code),
    )

    # ---- Part 2's eighth assertion, and it is B's ------------------
    # Position is ROW_NUMBER() at read time and nothing is stored. The
    # proof is that the SECOND student's position changes from 2 to 1
    # when the first leaves, while their row is untouched -- same id,
    # same created_at. A stored position would have required an UPDATE
    # that nothing here performs.
    r = httpx.delete(f"{BASE}/offerings/{full}/waitlist", headers=bearer(first_token))
    check(
        "leave -> 200 reporting the position that was held",
        r.status_code == 200 and r.json()["position"] == 1,
        f"{r.status_code} {r.json()}",
    )

    r = httpx.get(f"{BASE}/offerings/{full}/waitlist", headers=bearer(holder_token))
    rows = r.json()
    moved_up = (
        len(rows) == 1
        and rows[0]["id"] == second_entry_id
        and rows[0]["position"] == 1
        and rows[0]["created_at"] == second_created
    )
    check(
        "GET waitlist reports position from ROW_NUMBER(), never stored",
        moved_up,
        "entry moved 2 -> 1 with id and created_at unchanged"
        if moved_up
        else str(rows),
    )
    if moved_up:
        # It was listed as pending against the routes not existing; it is
        # now measured, so it stops being pending.
        pending[:] = [
            p
            for p in pending
            if p != "GET waitlist reports position from ROW_NUMBER(), never stored"
        ]

    r = httpx.delete(f"{BASE}/offerings/{full}/waitlist", headers=bearer(first_token))
    check(
        "leaving twice -> 409 NOT_WAITLISTED",
        r.status_code == 409 and r.json()["detail"]["code"] == "NOT_WAITLISTED",
        f"{r.status_code} {r.json()}",
    )

    # ---- the seven promotion assertions: A's column ----------------
    # Probed behaviourally rather than assumed: fill the seat back up,
    # leave one student queued, drop the seat, and see whether the entry
    # was consumed.
    httpx.post(f"{BASE}/offerings/{full}/waitlist", headers=bearer(first_token))
    httpx.delete(f"{BASE}/offerings/{full}/drop", headers=bearer(holder_token))
    db = SessionLocal()
    try:
        still_queued = (
            db.query(WaitlistEntry)
            .filter(WaitlistEntry.course_offering_id == full)
            .count()
        )
    finally:
        db.close()

    promotion_exists = still_queued < 2

    why = (
        "promotion transaction does not exist yet -- A's column of "
        "Deadline 7. B's endpoints are complete and asserted above."
    )
    if not promotion_exists:
        # ONE failure, not seven. The deadline is half-built and the gate
        # is supposed to say so -- but it should say it once, precisely,
        # rather than reporting seven separate broken things.
        check(
            "promotion runs when a seat is dropped (A's column)",
            False,
            f"{still_queued} entries still queued after a drop -- {why}",
        )
        skip("promotion follows FIFO by (created_at, id)", why)
        skip("promotion respects the promoted student's course-load quota", why)
        skip("a quota-breaching candidate is SKIPPED, next eligible promoted", why)
        skip("2 concurrent drops -> exactly 2 DISTINCT promotions", why)
        skip("2 concurrent drops -> no entry promoted twice", why)
        skip("promotion DELETES the waitlist row, renumbering nothing", why)
        skip("promoted student's enrollment is an UPDATE of the DROPPED row", why)
    else:
        # ---------------------------------------------------------------
        # Promotion exists. The seven assertions A wrote as skips, plus an
        # eighth that B's response to item 9 made a condition of ratifying
        # it: SKIP LOCKED must be observable, or it ships unmeasured.
        # ---------------------------------------------------------------

        def enrollment_rows(student_id: int, offering_id: int) -> list:
            d = SessionLocal()
            try:
                return (
                    d.query(Enrollment)
                    .filter(
                        Enrollment.student_id == student_id,
                        Enrollment.course_offering_id == offering_id,
                    )
                    .all()
                )
            finally:
                d.close()

        def queued_ids(offering_id: int) -> list[int]:
            d = SessionLocal()
            try:
                return [
                    e.student_id
                    for e in d.query(WaitlistEntry)
                    .filter(WaitlistEntry.course_offering_id == offering_id)
                    .order_by(WaitlistEntry.created_at, WaitlistEntry.id)
                    .all()
                ]
            finally:
                d.close()

        def counters(offering_id: int) -> tuple[int, int]:
            """(enrolled_count, ACTIVE enrollment rows) — must always agree."""
            d = SessionLocal()
            try:
                off = d.get(CourseOffering, offering_id)
                active = (
                    d.query(Enrollment)
                    .filter(
                        Enrollment.course_offering_id == offering_id,
                        Enrollment.status == EnrollmentStatus.ACTIVE,
                    )
                    .count()
                )
                return off.enrolled_count, active
            finally:
                d.close()

        # ---- FIFO, the DELETE, and the UPDATE-not-INSERT trap ----------
        # One offering, capacity 1. The promoted student is deliberately
        # one who ALREADY DROPPED this offering, so they still own a
        # DROPPED enrollment row -- `enrollment_unique` is unconditional.
        # Promotion must UPDATE it. An INSERT would raise IntegrityError
        # from inside the transaction instead.
        off = make_offering(capacity=1)
        returner_id, returner_token = make_student()
        keeper_id, keeper_token = make_student()
        w2_id, w2_token = make_student()
        w3_id, w3_token = make_student()

        httpx.post(f"{BASE}/offerings/{off}/register", headers=bearer(returner_token))
        httpx.delete(f"{BASE}/offerings/{off}/drop", headers=bearer(returner_token))
        rows = enrollment_rows(returner_id, off)
        check(
            "setup: the returning student owns a DROPPED row",
            len(rows) == 1 and rows[0].status is EnrollmentStatus.DROPPED,
            f"{[(r.id, r.status.value) for r in rows]}",
        )

        httpx.post(f"{BASE}/offerings/{off}/register", headers=bearer(keeper_token))
        for tok in (returner_token, w2_token, w3_token):
            httpx.post(f"{BASE}/offerings/{off}/waitlist", headers=bearer(tok))
        check(
            "setup: three queued, oldest first",
            queued_ids(off) == [returner_id, w2_id, w3_id],
            str(queued_ids(off)),
        )

        before = queued_ids(off)
        httpx.delete(f"{BASE}/offerings/{off}/drop", headers=bearer(keeper_token))
        after = queued_ids(off)

        check(
            "promotion follows FIFO by (created_at, id)",
            after == [w2_id, w3_id],
            f"{before} -> {after}; the oldest entry is the one consumed",
        )
        check(
            "promotion DELETES the waitlist row, renumbering nothing",
            len(after) == len(before) - 1 and after == before[1:],
            "exactly one row gone, the survivors unchanged and in order",
        )

        rows = enrollment_rows(returner_id, off)
        check(
            "promoted student's enrollment is an UPDATE of the DROPPED row",
            len(rows) == 1 and rows[0].status is EnrollmentStatus.ACTIVE,
            f"{len(rows)} row(s), status {rows[0].status.value if rows else '-'}",
        )
        counter, active = counters(off)
        check(
            "the seat moved rather than vanishing: counter == active rows",
            counter == active == 1,
            f"enrolled_count={counter} active_rows={active}",
        )

        # ---- the quota gate, and the skip it causes --------------------
        # (STUDENT, COURSE) is lowered to 1 for this stretch and restored
        # to the SEEDED CONSTANT afterwards -- never to "whatever was
        # found at startup", which is the self-inflicted loop session 14
        # recorded when a crashed run made a corrupted value look seeded.
        SEEDED_COURSE_QUOTA = 6
        db = SessionLocal()
        try:
            admin_email = db.query(User).filter(User.role == Role.ADMIN).first().email
        finally:
            db.close()
        admin = bearer(login(admin_email))

        httpx.put(
            f"{BASE}/admin/quotas/STUDENT/COURSE", headers=admin, json={"max_units": 1}
        )
        try:
            other = make_offering(capacity=5, days="U", start="08:00", end="09:00")
            quota_off = make_offering(capacity=1, days="S", start="08:00", end="09:00")
            busy_id, busy_token = make_student()
            free_id, free_token = make_student()
            holder2_id, holder2_token = make_student()

            # `busy` spends their single permitted enrollment elsewhere.
            r = httpx.post(
                f"{BASE}/offerings/{other}/register", headers=bearer(busy_token)
            )
            check("setup: the busy student is at their cap of 1", r.status_code == 201,
                  str(r.status_code))

            httpx.post(
                f"{BASE}/offerings/{quota_off}/register", headers=bearer(holder2_token)
            )

            # Both queue. `busy` is FIRST, so FIFO alone would promote a
            # student who cannot legally take the seat.
            r = httpx.post(
                f"{BASE}/offerings/{quota_off}/waitlist", headers=bearer(busy_token)
            )
            check(
                "a student at their course cap may still QUEUE",
                r.status_code == 201,
                f"{r.status_code} -- queueing holds nothing, so it costs no quota",
            )
            httpx.post(
                f"{BASE}/offerings/{quota_off}/waitlist", headers=bearer(free_token)
            )

            httpx.delete(
                f"{BASE}/offerings/{quota_off}/drop", headers=bearer(holder2_token)
            )

            busy_rows = enrollment_rows(busy_id, quota_off)
            free_rows = enrollment_rows(free_id, quota_off)
            check(
                "promotion respects the promoted student's course-load quota",
                not any(r.status is EnrollmentStatus.ACTIVE for r in busy_rows),
                "the over-quota candidate was NOT seated",
            )
            check(
                "a quota-breaching candidate is SKIPPED, next eligible promoted",
                len(free_rows) == 1
                and free_rows[0].status is EnrollmentStatus.ACTIVE
                and queued_ids(quota_off) == [busy_id],
                f"free student seated; queue still holds {queued_ids(quota_off)}",
            )
        finally:
            httpx.put(
                f"{BASE}/admin/quotas/STUDENT/COURSE",
                headers=admin,
                json={"max_units": SEEDED_COURSE_QUOTA},
            )

        # ---- 2 concurrent drops, 3 queued ------------------------------
        race_off = make_offering(capacity=2, days="F", start="16:00", end="17:00")
        h1_id, h1_token = make_student()
        h2_id, h2_token = make_student()
        queue_tokens = []
        queue_ids = []
        for _ in range(3):
            sid, tok = make_student()
            queue_ids.append(sid)
            queue_tokens.append(tok)

        httpx.post(f"{BASE}/offerings/{race_off}/register", headers=bearer(h1_token))
        httpx.post(f"{BASE}/offerings/{race_off}/register", headers=bearer(h2_token))
        for tok in queue_tokens:
            httpx.post(f"{BASE}/offerings/{race_off}/waitlist", headers=bearer(tok))

        # Threads rather than the asyncio harness: this is a gate, and it
        # asserts. Benchmark 4 is the same race on the harness, measured
        # over trials against a build with the offering lock removed.
        gate = threading.Barrier(2)

        def drop_racer(token: str) -> None:
            gate.wait()
            httpx.delete(f"{BASE}/offerings/{race_off}/drop", headers=bearer(token))

        threads = [
            threading.Thread(target=drop_racer, args=(t,))
            for t in (h1_token, h2_token)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        remaining = queued_ids(race_off)
        promoted = [
            sid
            for sid in queue_ids
            if any(
                r.status is EnrollmentStatus.ACTIVE
                for r in enrollment_rows(sid, race_off)
            )
        ]
        counter, active = counters(race_off)

        check(
            "2 concurrent drops -> exactly 2 DISTINCT promotions",
            len(promoted) == 2 and len(set(promoted)) == 2,
            f"promoted {promoted}, still queued {remaining}",
        )
        check(
            "2 concurrent drops -> no entry promoted twice",
            len(remaining) == 1
            and counter == active == 2
            and promoted == queue_ids[:2],
            f"enrolled_count={counter} active={active}, FIFO order preserved",
        )

        # ---- item 9's actual claim, and nothing above tests it ---------
        # B's condition on ratifying item 9 (session 15). The two columns
        # above never lock a candidate's row, so SKIP LOCKED never fires
        # and the mechanism ships unmeasured. Here the first candidate's
        # user row is deliberately HELD by another session.
        #
        # Deterministic on purpose: holding a lock is not a race, so this
        # needs one run rather than twenty-five.
        skip_off = make_offering(capacity=1, days="M", start="20:00", end="21:00")
        held_id, held_token = make_student()
        next_id, next_token = make_student()
        holder3_id, holder3_token = make_student()

        httpx.post(f"{BASE}/offerings/{skip_off}/register", headers=bearer(holder3_token))
        httpx.post(f"{BASE}/offerings/{skip_off}/waitlist", headers=bearer(held_token))
        httpx.post(f"{BASE}/offerings/{skip_off}/waitlist", headers=bearer(next_token))

        HOLD_SECONDS = 5.0
        lock_taken = threading.Event()

        def hold_user_row() -> None:
            d = SessionLocal()
            try:
                d.execute(
                    text("SELECT id FROM users WHERE id = :uid FOR UPDATE"),
                    {"uid": held_id},
                )
                lock_taken.set()
                time.sleep(HOLD_SECONDS)
                d.rollback()
            finally:
                d.close()

        holder_thread = threading.Thread(target=hold_user_row, daemon=True)
        holder_thread.start()
        lock_taken.wait(timeout=10)

        started = time.perf_counter()
        httpx.delete(f"{BASE}/offerings/{skip_off}/drop", headers=bearer(holder3_token))
        elapsed = time.perf_counter() - started

        still_queued = queued_ids(skip_off)
        next_rows = enrollment_rows(next_id, skip_off)

        check(
            "a candidate whose user row is LOCKED is skipped, not waited for",
            still_queued == [held_id],
            f"queue still holds {still_queued} -- the locked candidate kept its place",
        )
        check(
            "the next eligible entry is promoted instead",
            len(next_rows) == 1 and next_rows[0].status is EnrollmentStatus.ACTIVE,
            "second candidate seated while the first was unavailable",
        )
        check(
            "promotion COMPLETES while the row is still held (SKIP LOCKED)",
            elapsed < HOLD_SECONDS / 2,
            f"drop returned in {elapsed:.2f}s against a {HOLD_SECONDS:.0f}s hold "
            "-- it did not wait",
        )
        holder_thread.join(timeout=HOLD_SECONDS + 2)

        # ---- REGRESSION: a seat and a queue place are exclusive --------
        #
        # Found while reviewing the promotion transaction, reproduced end
        # to end, and it lost a seat silently:
        #
        #   X queues for a full offering. A drop frees the seat but
        #   promotion SKIPS X -- here by schedule clash, and the choice of
        #   skip reason matters. X clears the clash and registers for the
        #   free seat DIRECTLY. `register` did not clear the queue entry,
        #   so X held a seat AND a place. The next drop promoted X again:
        #   enrolled_count 2 against 1 ACTIVE row, a seat the counter
        #   called taken and no student held.
        #
        # **Why the skip reason matters, and why this test uses a clash.**
        # The first attempt to reproduce it used a QUOTA skip and came
        # back clean -- the candidate was at their cap only *because* the
        # seat they already held counted toward it, so the quota gate
        # refused the second promotion by accident. A clash-based skip
        # leaves the student far below their cap and nothing shields it.
        # A regression test that reproduced it the first way would pass
        # against the broken build, which is this project's oldest lesson.
        reg_off = make_offering(capacity=1, start="09:00", end="10:00", days="M")
        reg_clash = make_offering(capacity=5, start="09:00", end="10:00", days="M")
        rx_id, rx_tok = make_student()
        rh_id, rh_tok = make_student()
        rz_id, rz_tok = make_student()

        httpx.post(f"{BASE}/offerings/{reg_off}/register", headers=bearer(rh_tok))
        httpx.post(f"{BASE}/offerings/{reg_off}/waitlist", headers=bearer(rx_tok))
        httpx.post(f"{BASE}/offerings/{reg_clash}/register", headers=bearer(rx_tok))
        httpx.delete(f"{BASE}/offerings/{reg_off}/drop", headers=bearer(rh_tok))
        httpx.delete(f"{BASE}/offerings/{reg_clash}/drop", headers=bearer(rx_tok))
        r = httpx.post(f"{BASE}/offerings/{reg_off}/register", headers=bearer(rx_tok))

        db = SessionLocal()
        try:
            still_queued = db.query(WaitlistEntry).filter(
                WaitlistEntry.student_id == rx_id,
                WaitlistEntry.course_offering_id == reg_off,
            ).count()
        finally:
            db.close()
        check(
            "registering directly CLEARS that student's queue place",
            r.status_code == 201 and still_queued == 0,
            f"register={r.status_code}, {still_queued} queue entries left",
        )

        # Drive the second promotion that used to double-count.
        db = SessionLocal()
        try:
            o = db.get(CourseOffering, reg_off)
            o.capacity = 2
            db.commit()
        finally:
            db.close()
        httpx.post(f"{BASE}/offerings/{reg_off}/register", headers=bearer(rz_tok))
        httpx.delete(f"{BASE}/offerings/{reg_off}/drop", headers=bearer(rz_tok))

        db = SessionLocal()
        try:
            o = db.get(CourseOffering, reg_off)
            active = db.query(Enrollment).filter(
                Enrollment.course_offering_id == reg_off,
                Enrollment.status == EnrollmentStatus.ACTIVE,
            ).count()
            counter = o.enrolled_count
        finally:
            db.close()
        check(
            "no double-promotion: enrolled_count == ACTIVE rows",
            counter == active,
            f"counter={counter} active={active} "
            "-- Deadline 8's reconciliation query, run early",
        )


# ===========================================================================
# cleanup
# ===========================================================================

print()
db = SessionLocal()
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
    db.query(User).filter(User.id.in_(made_users)).delete(synchronize_session=False)
    db.commit()

    check("test users removed", db.query(User).count() == 3, str(db.query(User).count()))
    check("test offerings removed", db.query(CourseOffering).count() == 1, "")
    check("no waitlist rows left behind", db.query(WaitlistEntry).count() == 0, "")
finally:
    db.close()

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)

if pending:
    print(f"all Part 1 checks passed; {len(pending)} promotion assertions PENDING")
    print("Deadline 7 is not built. This gate turns red the moment it is")
    print("half-built, which is what it is for.")
    sys.exit(0)

print("all checks passed")
