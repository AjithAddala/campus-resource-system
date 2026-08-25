"""Exercise `POST /courses` and `POST /offerings` against the running API.

Run inside the container:

    docker compose exec app python scripts/check_catalog.py

Exits non-zero on the first failure. Tenth gate in the project, and the
only one written after the plan closed -- these two endpoints belonged to
no deadline, which is why they did not exist until now.

The assertions are grouped by what is actually being defended:

  AUTHORIZATION  creating catalogue rows is [ADMIN], like every other
                 create in the system
  VALIDATION     the 422s that keep `_times_overlap` a string comparison
                 and `_days_overlap` a set intersection -- both are
                 correct ONLY for the vocabulary `CourseOfferingCreate`
                 enforces, so these are not cosmetic
  REFERENCES     course_id / instructor_id resolved before the insert, so
                 the caller learns WHICH id was wrong
  UNIQUENESS     duplicate code -> 409, including under a race, because
                 the check is a caught IntegrityError and not a pre-read
  END-TO-END     a course and an offering created entirely over HTTP can
                 then be registered for -- the seed script is no longer
                 the only way to get a section

Everything it makes, it deletes.
"""
import sys
import threading
import uuid
from pathlib import Path

import httpx

# Running a file inside scripts/ puts scripts/ on sys.path, not the repo
# root, so `app` would not import. Same job as alembic.ini's prepend_sys_path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._db import SessionLocal  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models import (  # noqa: E402
    Course,
    CourseOffering,
    Enrollment,
    Role,
    User,
)

BASE = "http://localhost:8000/api/v1"
SEED_PASSWORD = "campus123"
TEST_PASSWORD = "check-password-123"

failures = []
made_users: list[int] = []
made_courses: list[int] = []
made_offerings: list[int] = []


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


def code_of(r: httpx.Response) -> str | None:
    detail = r.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def new_code() -> str:
    return f"TST{uuid.uuid4().hex[:6].upper()}"


def post_course(token: str, **body) -> httpx.Response:
    r = httpx.post(f"{BASE}/courses", headers=bearer(token), json=body, timeout=30)
    if r.status_code == 201:
        made_courses.append(r.json()["id"])
    return r


def post_offering(token: str, **overrides) -> httpx.Response:
    body = {
        "course_id": overrides.pop("course_id"),
        "instructor_id": overrides.pop("instructor_id"),
        "semester": "AUTUMN",
        "year": 2031,
        "start_time": "09:00",
        "end_time": "10:30",
        "days": "MWF",
        "capacity": 30,
    }
    body.update(overrides)
    r = httpx.post(f"{BASE}/offerings", headers=bearer(token), json=body, timeout=30)
    if r.status_code == 201:
        made_offerings.append(r.json()["id"])
    return r


def make_student() -> str:
    """One fresh STUDENT account, returned as a bearer token."""
    db = SessionLocal()
    try:
        email = f"check-{uuid.uuid4().hex[:12]}@iitk.ac.in"
        user = User(
            name="Check Student",
            email=email,
            password_hash=hash_password(TEST_PASSWORD),
            role=Role.STUDENT,
        )
        db.add(user)
        db.commit()
        made_users.append(user.id)
    finally:
        db.close()
    return login(email, TEST_PASSWORD)


def seeded_ids() -> tuple[int, int, int]:
    """(faculty_id, student_id, admin_id) from the seeded accounts."""
    db = SessionLocal()
    try:
        by_role = {
            u.role: u.id
            for u in db.query(User).filter(User.email.like("%@iitk.ac.in")).all()
        }
        return by_role[Role.FACULTY], by_role[Role.STUDENT], by_role[Role.ADMIN]
    finally:
        db.close()


student = login("student@iitk.ac.in")
faculty = login("faculty@iitk.ac.in")
admin = login("admin@iitk.ac.in")
faculty_id, student_id, admin_id = seeded_ids()

# --- AUTHORIZATION ------------------------------------------------------
# Same gate as POST /gpus and POST /rooms. Deadline 3's finding applies
# unchanged: the 403 must fire before the handler body, so a refused
# request must leave no row behind. The row counts below are that claim.
db = SessionLocal()
try:
    courses_before = db.query(Course).count()
    offerings_before = db.query(CourseOffering).count()
finally:
    db.close()

r = httpx.post(f"{BASE}/courses", json={"code": new_code(), "name": "No Token"})
check("POST /courses, no token -> 401", r.status_code == 401, str(r.status_code))

r = httpx.post(f"{BASE}/offerings", json={"course_id": 1, "instructor_id": 1})
check("POST /offerings, no token -> 401", r.status_code == 401, str(r.status_code))

r = post_course(student, code=new_code(), name="Student Attempt")
check("POST /courses as STUDENT -> 403", r.status_code == 403, str(r.status_code))

r = post_course(faculty, code=new_code(), name="Faculty Attempt")
check("POST /courses as FACULTY -> 403", r.status_code == 403, str(r.status_code))

r = post_offering(student, course_id=1, instructor_id=faculty_id)
check("POST /offerings as STUDENT -> 403", r.status_code == 403, str(r.status_code))

db = SessionLocal()
try:
    check(
        "403s wrote nothing: course count unchanged",
        db.query(Course).count() == courses_before,
        f"{courses_before} -> {db.query(Course).count()}",
    )
    check(
        "403s wrote nothing: offering count unchanged",
        db.query(CourseOffering).count() == offerings_before,
        f"{offerings_before} -> {db.query(CourseOffering).count()}",
    )
finally:
    db.close()

# --- COURSE CREATION ----------------------------------------------------
print()
code = new_code()
r = post_course(admin, code=code, name="Distributed Systems")
check("POST /courses as ADMIN -> 201", r.status_code == 201, str(r.status_code))
course_id = r.json()["id"] if r.status_code == 201 else None
check("201 body carries the code", r.json().get("code") == code, str(r.json()))
check(
    "created course is visible on GET /courses/{id}",
    httpx.get(f"{BASE}/courses/{course_id}", headers=bearer(student)).status_code == 200,
)

# Case folding is a correctness matter, not tidiness: `ix_courses_code` is
# case-sensitive, so without normalisation "cs641" and "CS641" both insert
# and the catalogue holds one course twice.
r = post_course(admin, code=code.lower(), name="Same Course, Lower Case")
check(
    "duplicate code differing only in case -> 409 COURSE_CODE_TAKEN",
    r.status_code == 409 and code_of(r) == "COURSE_CODE_TAKEN",
    f"{r.status_code} {code_of(r)}",
)

mixed = new_code()
r = post_course(admin, code=f"  {mixed.lower()}  ", name="  Spaced   Out  ")
check("code is upper-cased and trimmed", r.json().get("code") == mixed, str(r.json()))
check(
    "name whitespace is collapsed",
    r.json().get("name") == "Spaced Out",
    str(r.json().get("name")),
)

r = post_course(admin, code="X", name="Too Short")
check("code shorter than 2 chars -> 422", r.status_code == 422, str(r.status_code))

r = post_course(admin, code=new_code(), name="   ")
check("blank name -> 422", r.status_code == 422, str(r.status_code))

# --- UNIQUENESS UNDER A RACE -------------------------------------------
# The duplicate is a CAUGHT IntegrityError, not a pre-read, so two admins
# racing on one code must produce exactly one 201 and one 409 -- never a
# 500. A pre-check passes this suite sequentially and fails right here.
print()
raced = new_code()
race_results: list[int] = []
barrier = threading.Barrier(8)


def racer() -> None:
    barrier.wait()
    r = httpx.post(
        f"{BASE}/courses",
        headers=bearer(admin),
        json={"code": raced, "name": "Race"},
        timeout=30,
    )
    race_results.append(r.status_code)
    if r.status_code == 201:
        made_courses.append(r.json()["id"])


threads = [threading.Thread(target=racer) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()

print(f"    RACE  8 admins, one code: {sorted(race_results)}")
check(
    "8-way duplicate race -> exactly one 201",
    race_results.count(201) == 1,
    f"{race_results.count(201)} succeeded",
)
check(
    "8-way duplicate race -> seven 409s, zero 5xx",
    race_results.count(409) == 7 and not any(s >= 500 for s in race_results),
    f"409s={race_results.count(409)} 5xx={sum(1 for s in race_results if s >= 500)}",
)

db = SessionLocal()
try:
    check(
        "race left exactly one row",
        db.query(Course).filter(Course.code == raced).count() == 1,
        f"{db.query(Course).filter(Course.code == raced).count()} rows",
    )
finally:
    db.close()

# --- OFFERING VALIDATION ------------------------------------------------
# Every 422 below protects a comparison that is correct ONLY for the
# canonical form. Unpadded "9:00" sorts after "10:30" lexicographically;
# "Tu"/"Th" intersect on "T" and report a phantom clash.
print()
r = post_offering(admin, course_id=course_id, instructor_id=faculty_id)
check("POST /offerings as ADMIN -> 201", r.status_code == 201, str(r.status_code))
offering_id = r.json()["id"] if r.status_code == 201 else None
check(
    "new offering starts empty",
    r.json().get("enrolled_count") == 0 and r.json().get("seats_available") == 30,
    str(r.json()),
)

for label, override in [
    ("unpadded start_time '9:00'", {"start_time": "9:00"}),
    ("24:00 is not a valid hour", {"end_time": "24:00"}),
    ("minute 61", {"end_time": "10:61"}),
    ("multi-char day token 'TuTh'", {"days": "TuTh"}),
    ("unknown day code 'X'", {"days": "MXF"}),
    ("repeated day code 'MM'", {"days": "MM"}),
    ("empty days", {"days": ""}),
    ("capacity 0", {"capacity": 0}),
    ("negative capacity", {"capacity": -5}),
    ("start_time == end_time", {"start_time": "09:00", "end_time": "09:00"}),
    ("start_time after end_time", {"start_time": "11:00", "end_time": "10:00"}),
    ("year out of range", {"year": 1899}),
    ("non-alphabetic semester", {"semester": "AUT-2031"}),
]:
    r = post_offering(
        admin, course_id=course_id, instructor_id=faculty_id, **override
    )
    check(f"{label} -> 422", r.status_code == 422, str(r.status_code))

r = post_offering(admin, course_id=course_id, instructor_id=faculty_id, days="mwf")
check("lower-case days accepted and upper-cased", r.json().get("days") == "MWF", str(r.json().get("days")))

r = post_offering(
    admin, course_id=course_id, instructor_id=faculty_id, semester=" autumn "
)
check(
    "semester is trimmed and upper-cased",
    r.json().get("semester") == "AUTUMN",
    str(r.json().get("semester")),
)

# --- REFERENCES ---------------------------------------------------------
# One ForeignKeyViolation cannot say WHICH id was wrong, which is why both
# are resolved before the insert.
print()
r = post_offering(admin, course_id=99_999_999, instructor_id=faculty_id)
check("unknown course_id -> 404", r.status_code == 404, str(r.status_code))
check(
    "and the 404 names the course",
    "course" in str(r.json().get("detail", "")).lower(),
    str(r.json().get("detail")),
)

r = post_offering(admin, course_id=course_id, instructor_id=99_999_999)
check("unknown instructor_id -> 404", r.status_code == 404, str(r.status_code))
check(
    "and the 404 names the instructor",
    "instructor" in str(r.json().get("detail", "")).lower(),
    str(r.json().get("detail")),
)

r = post_offering(admin, course_id=course_id, instructor_id=student_id)
check(
    "STUDENT as instructor -> 409 INSTRUCTOR_NOT_FACULTY",
    r.status_code == 409 and code_of(r) == "INSTRUCTOR_NOT_FACULTY",
    f"{r.status_code} {code_of(r)}",
)

r = post_offering(admin, course_id=course_id, instructor_id=admin_id)
check(
    "ADMIN as instructor -> 409 INSTRUCTOR_NOT_FACULTY",
    r.status_code == 409 and code_of(r) == "INSTRUCTOR_NOT_FACULTY",
    f"{r.status_code} {code_of(r)}",
)

db = SessionLocal()
try:
    check(
        "every rejected offering wrote nothing",
        db.query(CourseOffering).filter(CourseOffering.course_id == course_id).count()
        == len([o for o in made_offerings if o is not None]),
        f"{db.query(CourseOffering).filter(CourseOffering.course_id == course_id).count()} rows",
    )
finally:
    db.close()

# --- END TO END ---------------------------------------------------------
# The point of the whole exercise: a section created entirely over HTTP
# behaves like a seeded one. Deadline 4's transaction is unchanged and
# does not know where its row came from.
print()
fresh = make_student()
r = httpx.post(
    f"{BASE}/offerings/{offering_id}/register", headers=bearer(fresh), timeout=30
)
check("student registers for the new offering -> 201", r.status_code == 201, str(r.status_code))

db = SessionLocal()
try:
    o = db.get(CourseOffering, offering_id)
    check("counter incremented under the offering lock", o.enrolled_count == 1, str(o.enrolled_count))
finally:
    db.close()

r = httpx.get(f"{BASE}/offerings/{offering_id}", headers=bearer(fresh))
check(
    "seats_available reflects the seat taken",
    r.json().get("seats_available") == 29,
    str(r.json().get("seats_available")),
)

r = httpx.get(f"{BASE}/courses/{course_id}/offerings", headers=bearer(fresh))
check(
    "created offering is listed under its course",
    any(o["id"] == offering_id for o in r.json()),
    f"{len(r.json())} offerings listed",
)

# A one-seat section created over HTTP still exhausts, which is the CHECK
# constraint and the offering lock doing their job on a row this endpoint
# made rather than the seed.
r = post_offering(
    admin, course_id=course_id, instructor_id=faculty_id, capacity=1,
    start_time="14:00", end_time="15:00", days="T",
)
tiny = r.json()["id"]
a, b = make_student(), make_student()
r1 = httpx.post(f"{BASE}/offerings/{tiny}/register", headers=bearer(a), timeout=30)
r2 = httpx.post(f"{BASE}/offerings/{tiny}/register", headers=bearer(b), timeout=30)
check("capacity-1 offering: first register -> 201", r1.status_code == 201, str(r1.status_code))
check(
    "capacity-1 offering: second -> 409 CAPACITY_EXHAUSTED",
    r2.status_code == 409 and code_of(r2) == "CAPACITY_EXHAUSTED",
    f"{r2.status_code} {code_of(r2)}",
)

# --- cleanup ------------------------------------------------------------
print()
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

    check("test courses removed", db.query(Course).count() == 1, f"{db.query(Course).count()}")
    check(
        "test offerings removed",
        db.query(CourseOffering).count() == 1,
        f"{db.query(CourseOffering).count()}",
    )
    check("test users removed", db.query(User).count() == 3, f"{db.query(User).count()}")
    check("no enrollments left behind", db.query(Enrollment).count() == 0, "")
finally:
    db.close()

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
