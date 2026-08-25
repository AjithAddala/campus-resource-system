"""Exercise the Deadline 6 quota rollout and admin endpoints.

Run inside the container:

    docker compose exec app python scripts/check_quotas.py

Exits non-zero on the first failure. Seventh gate in the project.

Deadline 4 proved the quota mechanism on ONE resource. Deadline 6 applies
it to the other two, so the assertions here are organised by the question
"is this the same invariant, or a new one?":

  ROOM quota      same mechanism, new resource  -> user lock + COUNT
  COURSE quota    same mechanism, new resource  -> user lock + COUNT
  GET /me/quota   the same numbers, read-only   -> must NOT take the lock
  admin policy    the row those gates read      -> ADMIN-gated write
  admin status    the column the D3/D4 gates read -> ADMIN-gated write

The room concurrency trial at the bottom is the only assertion that a
resource-lock-only build fails. Everything above it passes without the
user lock, because sequential requests never race -- the same reason
Deadline 4's checkpoint could not prove anything about locking on its
own.

Leaves the database at its post-seed state: no holds, no enrollments,
seeded quota values restored, rooms unblocked, clusters back to 8 and 4.
"""
import collections
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.security import hash_password  # noqa: E402
from scripts._db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Course,
    CourseOffering,
    Enrollment,
    GPUCluster,
    GPUReservation,
    Reservation,
    ReservationStatus,
    ResourceStatus,
    ResourceType,
    Role,
    RoleQuota,
    Room,
    User,
)

BASE = "http://localhost:8000/api/v1"
SEED_PASSWORD = "campus123"
TEST_PASSWORD = "check-password-123"

SEEDED_QUOTAS = [
    (Role.STUDENT, ResourceType.GPU, 2),
    (Role.FACULTY, ResourceType.GPU, 10),
    (Role.ADMIN, ResourceType.GPU, None),
    (Role.STUDENT, ResourceType.ROOM, 2),
    (Role.FACULTY, ResourceType.ROOM, 5),
    (Role.ADMIN, ResourceType.ROOM, None),
    (Role.STUDENT, ResourceType.COURSE, 6),
    (Role.ADMIN, ResourceType.COURSE, None),
]

failures = []
made_users: list[int] = []
made_offerings: list[int] = []


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


def code_of(r: httpx.Response) -> str | None:
    detail = r.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


SLOT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


def book(token: str, room_id: int, hour_offset: int) -> httpx.Response:
    start = SLOT + timedelta(hours=hour_offset)
    return httpx.post(
        f"{BASE}/rooms/{room_id}/reservations",
        json={
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(hours=1)).isoformat(),
        },
        headers=bearer(token),
        timeout=30,
    )


def make_student() -> tuple[str, int, str]:
    """A fresh STUDENT -> (email, id, token)."""
    db = SessionLocal()
    try:
        email = f"quota-{uuid.uuid4().hex[:12]}@iitk.ac.in"
        user = User(
            name="Quota Check",
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
    return email, uid, login(email, TEST_PASSWORD)


def set_quota_direct(role: Role, rtype: ResourceType, units: int | None) -> None:
    db = SessionLocal()
    try:
        row = (
            db.query(RoleQuota)
            .filter(RoleQuota.role == role, RoleQuota.resource_type == rtype)
            .one_or_none()
        )
        if row is None:
            db.add(RoleQuota(role=role, resource_type=rtype, max_units=units))
        else:
            row.max_units = units
        db.commit()
    finally:
        db.close()


def held_rooms(user_id: int) -> int:
    db = SessionLocal()
    try:
        return (
            db.query(Reservation)
            .filter(
                Reservation.user_id == user_id,
                Reservation.status == ReservationStatus.ACTIVE,
            )
            .count()
        )
    finally:
        db.close()


def reset_state() -> None:
    """No holds, no enrollments, nothing blocked, capacities as seeded."""
    db = SessionLocal()
    try:
        db.query(Reservation).delete(synchronize_session=False)
        db.query(GPUReservation).delete(synchronize_session=False)
        db.query(Enrollment).delete(synchronize_session=False)
        for c in db.query(GPUCluster).all():
            c.allocated = 0
            c.status = ResourceStatus.AVAILABLE
        for r in db.query(Room).all():
            r.status = ResourceStatus.AVAILABLE
        for o in db.query(CourseOffering).all():
            o.enrolled_count = 0
        db.commit()
    finally:
        db.close()


student = login("student@iitk.ac.in")
faculty = login("faculty@iitk.ac.in")
admin = login("admin@iitk.ac.in")

db = SessionLocal()
try:
    rooms = db.query(Room).order_by(Room.id).all()
    r1, r2 = rooms[0].id, rooms[1].id
    clusters = db.query(GPUCluster).order_by(GPUCluster.id).all()
    c1, c1_count = clusters[0].id, clusters[0].gpu_count
    seed_student_id = db.query(User).filter(User.email == "student@iitk.ac.in").one().id
    base_offering = db.query(CourseOffering).order_by(CourseOffering.id).first().id
    course_id = db.query(Course).order_by(Course.id).first().id
    faculty_id = db.query(User).filter(User.email == "faculty@iitk.ac.in").one().id
finally:
    db.close()

reset_state()

r = book(student, r1, 0)
check("room 1 of 2 -> 201", r.status_code == 201, str(r.status_code))
first_hold = r.json().get("id")

r = book(student, r2, 10)
check("room 2 of 2 -> 201", r.status_code == 201, str(r.status_code))

r = book(student, r1, 20)
check("room 3 -> 409", r.status_code == 409, str(r.status_code))
check("room 3 -> QUOTA_EXCEEDED", code_of(r) == "QUOTA_EXCEEDED", str(code_of(r)))
check("the refused booking created nothing", held_rooms(seed_student_id) == 2, str(held_rooms(seed_student_id)))

r = book(faculty, r1, 0)
check(
    "faculty booking the SAME slot -> INTERVAL_CONFLICT, not QUOTA",
    r.status_code == 409 and code_of(r) == "INTERVAL_CONFLICT",
    f"{r.status_code} {code_of(r)}",
)

r = httpx.delete(
    f"{BASE}/rooms/{r1}/reservations/{first_hold}", headers=bearer(student), timeout=30
)
check("cancel a hold -> 200", r.status_code == 200, str(r.status_code))
r = book(student, r1, 30)
check("after cancelling, a new booking succeeds", r.status_code == 201, str(r.status_code))

for i in range(3):
    r = book(admin, r2, 40 + i)
    check(f"admin unlimited: booking {i + 1} -> 201", r.status_code == 201, str(r.status_code))

reset_state()
r = httpx.get(f"{BASE}/me/quota", headers=bearer(student), timeout=30)
check("GET /me/quota -> 200", r.status_code == 200, str(r.status_code))
body = r.json()
by_type = {row["resource_type"]: row for row in body.get("quotas", [])}
check("me/quota reports all three resources", set(by_type) == {"GPU", "ROOM", "COURSE"}, str(set(by_type)))
check("me/quota reports the caller's role", body.get("role") == "STUDENT", str(body.get("role")))
check("student GPU limit is 2", by_type["GPU"]["limit"] == 2, str(by_type["GPU"]["limit"]))
check("student ROOM limit is 2", by_type["ROOM"]["limit"] == 2, str(by_type["ROOM"]["limit"]))
check("student COURSE limit is 6", by_type["COURSE"]["limit"] == 6, str(by_type["COURSE"]["limit"]))
check("nothing held after reset", all(row["held"] == 0 for row in by_type.values()), "")

book(student, r1, 0)
r = httpx.get(f"{BASE}/me/quota", headers=bearer(student), timeout=30)
by_type = {row["resource_type"]: row for row in r.json()["quotas"]}
check("me/quota reflects a real holding", by_type["ROOM"]["held"] == 1, str(by_type["ROOM"]["held"]))

r = httpx.get(f"{BASE}/me/quota", headers=bearer(faculty), timeout=30)
check("faculty me/quota -> 200 despite an unseeded pair", r.status_code == 200, str(r.status_code))
by_type = {row["resource_type"]: row for row in r.json()["quotas"]}
check(
    "(FACULTY, COURSE) reports configured=false, not unlimited",
    by_type["COURSE"]["configured"] is False and by_type["COURSE"]["unlimited"] is False,
    str(by_type["COURSE"]),
)
r = httpx.get(f"{BASE}/me/quota", headers=bearer(admin), timeout=30)
by_type = {row["resource_type"]: row for row in r.json()["quotas"]}
check(
    "admin GPU reports unlimited=true with a null limit",
    by_type["GPU"]["unlimited"] is True and by_type["GPU"]["limit"] is None,
    str(by_type["GPU"]),
)

locker = SessionLocal()
try:
    from sqlalchemy import select as _select

    locker.execute(
        _select(User.id).where(User.id == seed_student_id).with_for_update()
    )
    try:
        r = httpx.get(f"{BASE}/me/quota", headers=bearer(student), timeout=5)
        answered = r.status_code == 200
    except httpx.ReadTimeout:
        answered = False
    check("me/quota answers while the user row is locked (takes no lock)", answered, "")
finally:
    locker.rollback()
    locker.close()

r = httpx.get(f"{BASE}/me/quota", timeout=30)
check("me/quota with no token -> 401", r.status_code == 401, str(r.status_code))

r = httpx.get(f"{BASE}/admin/quotas/STUDENT/GPU", headers=bearer(student), timeout=30)
check("student reading policy -> 403", r.status_code == 403, str(r.status_code))
r = httpx.put(
    f"{BASE}/admin/quotas/STUDENT/GPU", json={"max_units": 99}, headers=bearer(student), timeout=30
)
check("student writing policy -> 403", r.status_code == 403, str(r.status_code))
db = SessionLocal()
try:
    unchanged = (
        db.query(RoleQuota)
        .filter(RoleQuota.role == Role.STUDENT, RoleQuota.resource_type == ResourceType.GPU)
        .one()
        .max_units
    )
finally:
    db.close()
check("the refused write changed nothing", unchanged == 2, str(unchanged))

r = httpx.get(f"{BASE}/admin/quotas/STUDENT/GPU", headers=bearer(admin), timeout=30)
check("admin reads policy -> 200, max_units 2", r.status_code == 200 and r.json()["max_units"] == 2, str(r.json() if r.status_code == 200 else r.status_code))

r = httpx.get(f"{BASE}/admin/quotas/FACULTY/COURSE", headers=bearer(admin), timeout=30)
check("unseeded pair -> 404, not a null limit", r.status_code == 404, str(r.status_code))

r = httpx.get(f"{BASE}/admin/quotas/WIZARD/GPU", headers=bearer(admin), timeout=30)
check("nonsense role -> 422 from the enum, before the query", r.status_code == 422, str(r.status_code))

r = httpx.put(
    f"{BASE}/admin/quotas/FACULTY/COURSE", json={"max_units": 3}, headers=bearer(admin), timeout=30
)
check("PUT creates a missing policy -> 200", r.status_code == 200, str(r.status_code))
r = httpx.get(f"{BASE}/admin/quotas/FACULTY/COURSE", headers=bearer(admin), timeout=30)
check("the created policy reads back as 3", r.status_code == 200 and r.json()["max_units"] == 3, str(r.status_code))
r = httpx.put(
    f"{BASE}/admin/quotas/FACULTY/COURSE", json={"max_units": None}, headers=bearer(admin), timeout=30
)
check("PUT null means unlimited", r.status_code == 200 and r.json()["max_units"] is None, str(r.status_code))
r = httpx.put(
    f"{BASE}/admin/quotas/FACULTY/COURSE", json={"max_units": 0}, headers=bearer(admin), timeout=30
)
check("PUT 0 is a valid policy (none at all)", r.status_code == 200 and r.json()["max_units"] == 0, str(r.status_code))
r = httpx.put(
    f"{BASE}/admin/quotas/FACULTY/COURSE", json={"max_units": -1}, headers=bearer(admin), timeout=30
)
check("PUT negative -> 422", r.status_code == 422, str(r.status_code))

reset_state()
book(student, r1, 0)
book(student, r2, 0)
check("setup: student holds 2 rooms", held_rooms(seed_student_id) == 2, str(held_rooms(seed_student_id)))
r = httpx.put(
    f"{BASE}/admin/quotas/STUDENT/ROOM", json={"max_units": 1}, headers=bearer(admin), timeout=30
)
check("lower the room quota to 1 -> 200", r.status_code == 200, str(r.status_code))
check("existing holds NOT evicted", held_rooms(seed_student_id) == 2, str(held_rooms(seed_student_id)))
r = book(student, r1, 50)
check("but a new booking is refused", r.status_code == 409 and code_of(r) == "QUOTA_EXCEEDED", f"{r.status_code} {code_of(r)}")
set_quota_direct(Role.STUDENT, ResourceType.ROOM, 2)

reset_state()
r = httpx.put(
    f"{BASE}/admin/quotas/STUDENT/COURSE", json={"max_units": 1}, headers=bearer(admin), timeout=30
)
check("set the course load limit to 1 -> 200", r.status_code == 200, str(r.status_code))

db = SessionLocal()
try:
    extra = CourseOffering(
        course_id=course_id,
        instructor_id=faculty_id,
        semester="AUTUMN",
        year=2026,
        start_time="14:00",
        end_time="15:30",
        days="TR",
        capacity=50,
        enrolled_count=0,
    )
    db.add(extra)
    db.flush()
    extra_id = extra.id
    made_offerings.append(extra_id)
    db.commit()
finally:
    db.close()

r = httpx.post(f"{BASE}/offerings/{base_offering}/register", headers=bearer(student), timeout=30)
check("course 1 of 1 -> 201", r.status_code == 201, str(r.status_code))
r = httpx.post(f"{BASE}/offerings/{extra_id}/register", headers=bearer(student), timeout=30)
check("course 2 -> 409", r.status_code == 409, str(r.status_code))
check("course 2 -> QUOTA_EXCEEDED, not SCHEDULE_CONFLICT", code_of(r) == "QUOTA_EXCEEDED", str(code_of(r)))

r = httpx.post(f"{BASE}/offerings/{base_offering}/register", headers=bearer(student), timeout=30)
check("re-registering the SAME offering -> ALREADY_ENROLLED, not QUOTA", code_of(r) == "ALREADY_ENROLLED", str(code_of(r)))

r = httpx.delete(f"{BASE}/offerings/{base_offering}/drop", headers=bearer(student), timeout=30)
check("drop -> 200", r.status_code == 200, str(r.status_code))
r = httpx.post(f"{BASE}/offerings/{extra_id}/register", headers=bearer(student), timeout=30)
check("after dropping, the other offering succeeds", r.status_code == 201, str(r.status_code))
set_quota_direct(Role.STUDENT, ResourceType.COURSE, 6)

reset_state()
r = httpx.patch(f"{BASE}/rooms/{r1}", json={"status": "BLOCKED"}, headers=bearer(student), timeout=30)
check("student PATCH /rooms -> 403", r.status_code == 403, str(r.status_code))

book(student, r1, 0)
r = httpx.patch(f"{BASE}/rooms/{r1}", json={"status": "BLOCKED"}, headers=bearer(admin), timeout=30)
check("admin blocks a room -> 200", r.status_code == 200 and r.json()["status"] == "BLOCKED", str(r.status_code))
check("blocking did NOT evict the existing hold", held_rooms(seed_student_id) == 1, str(held_rooms(seed_student_id)))
r = book(faculty, r1, 60)
check("booking a blocked room -> RESOURCE_BLOCKED", r.status_code == 409 and code_of(r) == "RESOURCE_BLOCKED", f"{r.status_code} {code_of(r)}")
r = httpx.patch(f"{BASE}/rooms/{r1}", json={"status": "AVAILABLE"}, headers=bearer(admin), timeout=30)
check("admin unblocks -> 200", r.status_code == 200, str(r.status_code))
r = book(faculty, r1, 60)
check("bookable again", r.status_code == 201, str(r.status_code))

r = httpx.patch(f"{BASE}/rooms/999999", json={"status": "BLOCKED"}, headers=bearer(admin), timeout=30)
check("PATCH a nonexistent room -> 404", r.status_code == 404, str(r.status_code))

reset_state()
r = httpx.post(f"{BASE}/gpus/{c1}/reservations", json={"gpu_count": 2}, headers=bearer(student), timeout=30)
check("setup: 2 units held on the cluster", r.status_code == 201, str(r.status_code))

r = httpx.patch(f"{BASE}/gpus/{c1}", json={"gpu_count": 1}, headers=bearer(admin), timeout=30)
check("shrink BELOW allocated -> 409", r.status_code == 409, str(r.status_code))
check("shrink below allocated -> CAPACITY_BELOW_ALLOCATED", code_of(r) == "CAPACITY_BELOW_ALLOCATED", str(code_of(r)))
db = SessionLocal()
try:
    still = db.get(GPUCluster, c1)
    check("the refused shrink changed nothing", still.gpu_count == c1_count and still.allocated == 2, f"count={still.gpu_count} allocated={still.allocated}")
finally:
    db.close()

r = httpx.patch(f"{BASE}/gpus/{c1}", json={"gpu_count": 2}, headers=bearer(admin), timeout=30)
check("shrink TO allocated -> 200 (the boundary is allowed)", r.status_code == 200, str(r.status_code))
r = httpx.post(f"{BASE}/gpus/{c1}/reservations", json={"gpu_count": 1}, headers=bearer(faculty), timeout=30)
check("the shrunk cluster is now full", r.status_code == 409 and code_of(r) == "CAPACITY_EXHAUSTED", f"{r.status_code} {code_of(r)}")

r = httpx.patch(f"{BASE}/gpus/{c1}", json={"gpu_count": c1_count}, headers=bearer(admin), timeout=30)
check("grow it back -> 200", r.status_code == 200, str(r.status_code))

r = httpx.patch(f"{BASE}/gpus/{c1}", json={"allocated": 0}, headers=bearer(admin), timeout=30)
db = SessionLocal()
try:
    check("allocated is not settable through PATCH", db.get(GPUCluster, c1).allocated == 2, str(db.get(GPUCluster, c1).allocated))
finally:
    db.close()

r = httpx.patch(f"{BASE}/gpus/{r1}", json={"status": "BLOCKED"}, headers=bearer(admin), timeout=30)
check("PATCH a room id via /gpus -> 404", r.status_code == 404, str(r.status_code))

TRIALS = 20
over_quota = 0
held_seen: collections.Counter = collections.Counter()
succeeded_seen: collections.Counter = collections.Counter()

for _ in range(TRIALS):
    reset_state()
    _, uid, token = make_student()
    book(token, r1, 0)

    barrier = threading.Barrier(2)
    results: list[int] = []
    lock = threading.Lock()

    def _race(room_id: int, offset: int) -> None:
        barrier.wait()
        resp = book(token, room_id, offset)
        with lock:
            results.append(resp.status_code)

    threads = [
        threading.Thread(target=_race, args=(r1, 70)),
        threading.Thread(target=_race, args=(r2, 80)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    held = held_rooms(uid)
    held_seen[held] += 1
    succeeded_seen[sum(1 for s in results if s == 201)] += 1
    if held > 2:
        over_quota += 1

print()
print(f"    ROOM QUOTA RACE  2 concurrent bookings, 2 different rooms, {TRIALS} trials")
print(f"      held rooms observed : {dict(sorted(held_seen.items()))}   (limit 2)")
print(f"      requests succeeding : {dict(sorted(succeeded_seen.items()))}")
print(f"      OVER-QUOTA TRIALS   : {over_quota}/{TRIALS}")
print()

check(f"room quota race: zero over-quota trials in {TRIALS}", over_quota == 0, f"{over_quota} trials ended with held > 2")
check("room quota race: exactly one booking succeeded each time", set(succeeded_seen) <= {1}, str(dict(succeeded_seen)))

reset_state()
db = SessionLocal()
try:
    db.query(Enrollment).delete(synchronize_session=False)
    if made_offerings:
        db.query(CourseOffering).filter(CourseOffering.id.in_(made_offerings)).delete(synchronize_session=False)
    db.query(User).filter(User.id.in_(made_users)).delete(synchronize_session=False)
    db.query(RoleQuota).delete(synchronize_session=False)
    db.add_all([RoleQuota(role=r, resource_type=t, max_units=u) for r, t, u in SEEDED_QUOTAS])
    for c in db.query(GPUCluster).all():
        c.allocated = 0
        c.status = ResourceStatus.AVAILABLE
    db.commit()

    check("test users removed", db.query(User).count() == 3, str(db.query(User).count()))
    check("test offerings removed", db.query(CourseOffering).count() == 1, str(db.query(CourseOffering).count()))
    check("reservations cleaned up", db.query(Reservation).count() == 0, "")
    check("enrollments cleaned up", db.query(Enrollment).count() == 0, "")
    check("quota policy restored to seeded values", db.query(RoleQuota).count() == len(SEEDED_QUOTAS), str(db.query(RoleQuota).count()))
    check(
        "clusters and rooms back to seeded state",
        all(c.allocated == 0 and c.status is ResourceStatus.AVAILABLE for c in db.query(GPUCluster).all())
        and all(r.status is ResourceStatus.AVAILABLE for r in db.query(Room).all())
        and db.get(GPUCluster, c1).gpu_count == c1_count,
        "",
    )
finally:
    db.close()

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
