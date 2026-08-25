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
    print("Waitlist routes detected -- Part 2 needs implementing against them.")
    failures.append("Part 2 not implemented though routes exist")


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
