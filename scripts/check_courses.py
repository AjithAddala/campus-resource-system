"""Exercise the Deadline 4 course registration path against the running API.

Run inside the container:

    docker compose exec app python scripts/check_courses.py

Exits non-zero on the first failure. Sixth gate in the project.

Course registration is the same transaction shape as the GPU flagship
with the offering row standing in for the cluster row -- it holds the
counter, so it is the row that gets locked. The assertions are grouped by
which invariant they are about:

  CAPACITY   keyed on the OFFERING -> the offering row lock
  SCHEDULE   keyed on the STUDENT  -> the user row lock
  IDENTITY   `enrollment_unique` is UNCONDITIONAL, so a dropped student
             still owns a row and re-registration is an UPDATE

Creates its own course, offerings and students so the seeded fixtures are
untouched, and deletes all of them at the end.
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

from app.core.security import hash_password  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Course,
    CourseOffering,
    Enrollment,
    EnrollmentStatus,
    Role,
    User,
)

BASE = "http://localhost:8000/api/v1"
SEED_PASSWORD = "campus123"
TEST_PASSWORD = "check-password-123"

failures = []
made_users: list[int] = []
made_offerings: list[int] = []
made_courses: list[int] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{'  -> ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def login(email: str, password: str = SEED_PASSWORD) -> str:
    r = httpx.post(f"{BASE}/auth/login", data={"username": email, "password": password})
    r.raise_for_status()
    return r.json()["access_token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def register(token: str, offering_id: int) -> httpx.Response:
    return httpx.post(
        f"{BASE}/offerings/{offering_id}/register", headers=bearer(token), timeout=30
    )


def drop(token: str, offering_id: int) -> httpx.Response:
    return httpx.delete(
        f"{BASE}/offerings/{offering_id}/drop", headers=bearer(token), timeout=30
    )


def code_of(r: httpx.Response) -> str | None:
    detail = r.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def make_offering(capacity: int, days: str, start: str, end: str) -> int:
    """A section to register against. Times must be zero-padded."""
    db = SessionLocal()
    try:
        instructor = db.query(User).filter(User.role == Role.FACULTY).first()
        course = Course(code=f"TST{uuid.uuid4().hex[:6].upper()}", name="Check Course")
        db.add(course)
        db.flush()
        offering = CourseOffering(
            course_id=course.id,
            instructor_id=instructor.id,
            semester="AUTUMN",
            year=2031,
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


def make_students(n: int) -> list[str]:
    """n fresh STUDENT accounts, returned as bearer tokens."""
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


def offering_state(offering_id: int) -> tuple[int, int]:
    db = SessionLocal()
    try:
        o = db.get(CourseOffering, offering_id)
        return o.enrolled_count, o.capacity
    finally:
        db.close()


def active_count(offering_id: int) -> int:
    db = SessionLocal()
    try:
        return (
            db.query(Enrollment)
            .filter(
                Enrollment.course_offering_id == offering_id,
                Enrollment.status == EnrollmentStatus.ACTIVE,
            )
            .count()
        )
    finally:
        db.close()


student = login("student@iitk.ac.in")
faculty = login("faculty@iitk.ac.in")
admin = login("admin@iitk.ac.in")

# MWF 09:00-10:30, roomy.
main_id = make_offering(50, "MWF", "09:00", "10:30")

# --- the boundary -------------------------------------------------------
r = httpx.post(f"{BASE}/offerings/{main_id}/register")
check("no token -> 401", r.status_code == 401, str(r.status_code))

# STUDENT-only, and that is why (FACULTY, COURSE) has no quota row: the
# pair is unreachable behind this 403.
r = register(faculty, main_id)
check("faculty -> 403 on register", r.status_code == 403, str(r.status_code))
r = register(admin, main_id)
check("admin -> 403 on register", r.status_code == 403, str(r.status_code))
check("the 403s enrolled nobody", offering_state(main_id)[0] == 0, str(offering_state(main_id)))

# --- the happy path -----------------------------------------------------
r = register(student, main_id)
check("student registers -> 201", r.status_code == 201, str(r.status_code))
enrollment = r.json() if r.status_code == 201 else {}
first_enrollment_id = enrollment.get("id")
check("enrollment is ACTIVE", enrollment.get("status") == "ACTIVE", str(enrollment.get("status")))
check("counter incremented", offering_state(main_id)[0] == 1, str(offering_state(main_id)))

r = httpx.get(f"{BASE}/offerings/{main_id}", headers=bearer(student))
check(
    "seats_available reflects the write",
    r.json()["seats_available"] == r.json()["capacity"] - 1,
    str(r.json()["seats_available"]),
)

# --- IDENTITY: duplicate, and the dropped-row trap ----------------------
r = register(student, main_id)
check(
    "duplicate registration -> 409 ALREADY_ENROLLED",
    r.status_code == 409 and code_of(r) == "ALREADY_ENROLLED",
    f"{r.status_code} {code_of(r)}",
)
check("duplicate did not touch the counter", offering_state(main_id)[0] == 1, str(offering_state(main_id)))

r = drop(student, main_id)
check("drop -> 200", r.status_code == 200, str(r.status_code))
check("status is DROPPED", r.json().get("status") == "DROPPED", str(r.json().get("status")))
check("counter decremented", offering_state(main_id)[0] == 0, str(offering_state(main_id)))

# Idempotent, like GPU cancel: the decrement happens only on the
# ACTIVE -> DROPPED transition, so a repeat is a no-op rather than a
# second decrement that would drive enrolled_count negative and trip
# `offering_enrollment_sane` as a 500.
r = drop(student, main_id)
check("drop twice -> 200, same row", r.status_code == 200, str(r.status_code))
check("counter NOT double-decremented", offering_state(main_id)[0] == 0, str(offering_state(main_id)))

# THE TRAP. `enrollment_unique` has no `WHERE status = 'ACTIVE'`, so the
# dropped student STILL OWNS A ROW. Re-registering must UPDATE it. An
# implementation that INSERTs here raises IntegrityError from inside the
# transaction -- a 500 that looks like a mysterious bug rather than the
# design consequence it is. The proof is the enrollment id: same row.
r = register(student, main_id)
check("re-registration after a drop -> 201", r.status_code == 201, str(r.status_code))
check(
    "re-registration UPDATED the existing row (same id)",
    r.json().get("id") == first_enrollment_id,
    f"{r.json().get('id')} vs {first_enrollment_id}",
)
db = SessionLocal()
try:
    rows = (
        db.query(Enrollment)
        .filter(Enrollment.course_offering_id == main_id)
        .count()
    )
finally:
    db.close()
check("still exactly one enrollment row for this student", rows == 1, f"{rows} rows")
check("counter back to 1", offering_state(main_id)[0] == 1, str(offering_state(main_id)))

# --- SCHEDULE: keyed on the student -------------------------------------
# Overlapping: MWF 10:00-11:00 against the held MWF 09:00-10:30.
clash_id = make_offering(50, "MWF", "10:00", "11:00")
r = register(student, clash_id)
check(
    "overlapping schedule -> 409 SCHEDULE_CONFLICT",
    r.status_code == 409 and code_of(r) == "SCHEDULE_CONFLICT",
    f"{r.status_code} {code_of(r)}",
)
check("the conflict enrolled nobody", offering_state(clash_id)[0] == 0, str(offering_state(clash_id)))

# Adjacent must SUCCEED: 10:30-12:00 starts exactly when the held one
# ends. Same half-open reasoning as the room exclusion constraint.
adjacent_id = make_offering(50, "MWF", "10:30", "12:00")
r = register(student, adjacent_id)
check("adjacent times -> 201", r.status_code == 201, str(r.status_code))

# Same clock, different days -> no conflict.
other_days_id = make_offering(50, "TR", "09:00", "10:30")
r = register(student, other_days_id)
check("same time, different days -> 201", r.status_code == 201, str(r.status_code))

# A dropped enrollment must stop conflicting, or a student could never
# swap one section for another.
#
# BOTH held offerings have to go, and the first run of this script got
# that wrong: it dropped only `main` (MWF 09:00-10:30) and expected the
# clashing 10:00-11:00 to become registrable -- but `adjacent` is MWF
# 10:30-12:00, which overlaps 10:00-11:00 between 10:30 and 11:00. The
# 409 was correct and the assertion was wrong. Recorded rather than
# quietly fixed, because a test that has to be talked out of a true
# failure is worth more attention than one that passes.
drop(student, main_id)
drop(student, adjacent_id)
r = register(student, clash_id)
check(
    "after dropping the clashing one, registration succeeds",
    r.status_code == 201,
    str(r.status_code),
)

for oid in (clash_id, other_days_id):
    drop(student, oid)

# --- CAPACITY: keyed on the offering ------------------------------------
tiny_id = make_offering(1, "S", "08:00", "09:00")
tokens = make_students(2)
r = register(tokens[0], tiny_id)
check("first student takes the only seat -> 201", r.status_code == 201, str(r.status_code))
r = register(tokens[1], tiny_id)
check(
    "second -> 409 CAPACITY_EXHAUSTED",
    r.status_code == 409 and code_of(r) == "CAPACITY_EXHAUSTED",
    f"{r.status_code} {code_of(r)}",
)
check("counter still 1", offering_state(tiny_id)[0] == 1, str(offering_state(tiny_id)))

# --- routing and misuse -------------------------------------------------
r = register(student, 999999)
check("nonexistent offering -> 404", r.status_code == 404, str(r.status_code))
r = drop(student, 999999)
check("dropping a nonexistent offering -> 404", r.status_code == 404, str(r.status_code))
r = drop(tokens[1], tiny_id)
check(
    "dropping something never registered -> 409 NOT_ENROLLED",
    r.status_code == 409 and code_of(r) == "NOT_ENROLLED",
    f"{r.status_code} {code_of(r)}",
)

# =======================================================================
# CONCURRENCY. Everything above passes with no locks at all.
# =======================================================================

# --- BENCHMARK 1 (capacity), in miniature -------------------------------
# N students, K seats, all released together. `enrolled_count` is read
# and written inside the offering lock, so exactly K may commit. Without
# that lock, several requests read the same count and the section
# oversells -- and `offering_enrollment_sane` would then reject the
# overshoot as a 500 rather than letting it through silently.
#
# Deadline 5 scales this to 500 requests on 50 seats with an asyncio
# harness; this is the same assertion at a size a thread pool can drive.
SEATS = 5
RACERS = 20
race_id = make_offering(SEATS, "U", "07:00", "08:00")
race_tokens = make_students(RACERS)

barrier = threading.Barrier(RACERS)
results: list[int] = []
lock = threading.Lock()


def _seat_race(token: str) -> None:
    barrier.wait()
    resp = register(token, race_id)
    with lock:
        results.append(resp.status_code)


threads = [threading.Thread(target=_seat_race, args=(t,)) for t in race_tokens]
for t in threads:
    t.start()
for t in threads:
    t.join()

enrolled, capacity = offering_state(race_id)
tally = dict(collections.Counter(results))
print()
print(f"    BENCHMARK 1 (mini)  {RACERS} concurrent registrations, {SEATS} seats: {tally}")
print(f"      enrolled_count = {enrolled}   active rows = {active_count(race_id)}")
print()
check(f"exactly {SEATS} registrations succeeded", tally.get(201, 0) == SEATS, str(tally))
check("enrolled_count never exceeded capacity", enrolled <= capacity, f"{enrolled}/{capacity}")
check("no 500s under contention", 500 not in tally, str(tally))

# The Deadline 8 reconciliation query, run early: derived state must agree
# with the rows it is derived from. Any path that updated one without the
# other shows up here and nowhere else.
check(
    "reconciliation: enrolled_count == COUNT(active enrollments)",
    enrolled == active_count(race_id),
    f"counter={enrolled} rows={active_count(race_id)}",
)

# --- schedule conflict under concurrency --------------------------------
# The reason registration takes the USER lock at Deadline 4 rather than
# waiting for the course-load quota at Deadline 6. Two clashing offerings
# share no offering row, so the offering lock cannot see the conflict --
# exactly the cross-cluster GPU quota race, on a different invariant.
#
# Run as trials, because a single race is a coin flip and can pass
# against a build with no user lock at all. (Benchmark 2 taught this the
# hard way: its first single-shot run reported a clean result against the
# deliberately broken build.)
TRIALS = 15
double_booked = 0
clash_a = make_offering(50, "MWF", "13:00", "14:00")
clash_b = make_offering(50, "MWF", "13:30", "14:30")
clash_tokens = make_students(TRIALS)

for token in clash_tokens:
    barrier2 = threading.Barrier(2)
    trial: list[int] = []
    tlock = threading.Lock()

    def _clash_race(offering_id: int, tok: str = token) -> None:
        barrier2.wait()
        resp = register(tok, offering_id)
        with tlock:
            trial.append(resp.status_code)

    ts = [
        threading.Thread(target=_clash_race, args=(oid,)) for oid in (clash_a, clash_b)
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    if trial.count(201) > 1:
        double_booked += 1

print()
print(
    f"    SCHEDULE RACE  {TRIALS} trials, 2 clashing offerings each: "
    f"{double_booked}/{TRIALS} students ended up double-booked"
)
print()
check(
    f"schedule race: zero double-bookings in {TRIALS} trials",
    double_booked == 0,
    f"{double_booked} students hold two clashing offerings",
)

# --- cleanup ------------------------------------------------------------
db = SessionLocal()
try:
    db.query(Enrollment).filter(
        Enrollment.course_offering_id.in_(made_offerings)
    ).delete(synchronize_session=False)
    db.query(CourseOffering).filter(CourseOffering.id.in_(made_offerings)).delete(
        synchronize_session=False
    )
    db.query(Course).filter(Course.id.in_(made_courses)).delete(
        synchronize_session=False
    )
    db.query(User).filter(User.id.in_(made_users)).delete(synchronize_session=False)
    db.commit()

    check("test offerings removed", db.query(CourseOffering).count() == 1, "")
    check("test users removed", db.query(User).count() == 3, "")
    check("no enrollments left behind", db.query(Enrollment).count() == 0, "")
finally:
    db.close()

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
