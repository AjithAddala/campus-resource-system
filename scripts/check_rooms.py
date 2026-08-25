"""Exercise the Deadline 3 room reservation path against the running API.

Run inside the container:

    docker compose exec app python scripts/check_rooms.py

Exits non-zero on the first failure, so it is a gate rather than
something to read. Fourth script in the project written that way.

The claim under test is narrower than it looks. Rooms are the ONE
resource whose invariant Postgres enforces directly: two concurrent
bookings of the same slot cannot both commit, whatever the application
does, because `no_overlapping_room_reservations` is an EXCLUDE USING gist
constraint. So the assertions here fall into two groups --

  * what the CONSTRAINT guarantees (overlap, adjacency, cancelled rows),
    which no application bug can break, and
  * what the SERVICE LAYER guarantees (the resource_type check, the
    BLOCKED gate), which nothing in the database enforces at all and
    which are therefore the only places application code can be wrong.

The second group is where the bugs would be, and it is deliberately
tested hardest.

The database is left exactly at its post-seed counts.
"""
import collections
import datetime as dt
import sys
import threading
import uuid
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

# Running a file inside scripts/ puts scripts/ on sys.path, not the repo
# root, so `app` would not import. Same job as alembic.ini's prepend_sys_path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.security import hash_password  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    GPUCluster,
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

failures = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{'  -> ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def login(email: str) -> str:
    r = httpx.post(
        f"{BASE}/auth/login", data={"username": email, "password": SEED_PASSWORD}
    )
    r.raise_for_status()
    return r.json()["access_token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# A window far enough out that it cannot collide with anything a previous
# run left behind, and unique per run.
DAY = dt.datetime(2031, 3, 3, tzinfo=dt.timezone.utc) + dt.timedelta(
    days=uuid.uuid4().int % 500
)


def at(hour: int) -> str:
    return (DAY + dt.timedelta(hours=hour)).isoformat()


def book(token: str, room_id: int, start: int, end: int) -> httpx.Response:
    return httpx.post(
        f"{BASE}/rooms/{room_id}/reservations",
        json={"start_time": at(start), "end_time": at(end)},
        headers=bearer(token),
        timeout=30,
    )


def code_of(r: httpx.Response) -> str | None:
    detail = r.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


student = login("student@iitk.ac.in")
admin = login("admin@iitk.ac.in")

made_users: list[int] = []


def make_students(n: int) -> list[str]:
    """n fresh STUDENT accounts, as bearer tokens.

    **Added at Deadline 6, and the reason is a regression this gate would
    otherwise have hidden.** The barrier race below fires N identical
    bookings at one room; before Deadline 6 they all used the seed
    student's token, which was fine because `reserve_room` took no user
    lock. It takes one now, for the room quota — so N requests from ONE
    account would serialize on that user's row and reach the exclusion
    constraint one at a time. The test would still pass, and it would be
    measuring the user lock instead of the constraint it was written for.

    Exactly the trap `check_gpus.py` fell into at Deadline 4: *"a capacity
    race in which every racer shares one account is not a capacity
    test."* Same fix, arriving on a different path.
    """
    db = SessionLocal()
    emails = []
    try:
        # Seeded password, so this script's existing single-argument
        # `login()` works on these accounts unchanged.
        hashed = hash_password(SEED_PASSWORD)
        for _ in range(n):
            email = f"room-{uuid.uuid4().hex[:12]}@iitk.ac.in"
            user = User(
                name="Room Check", email=email, password_hash=hashed, role=Role.STUDENT
            )
            db.add(user)
            db.flush()
            made_users.append(user.id)
            emails.append(email)
        db.commit()
    finally:
        db.close()
    return [login(e) for e in emails]


SEEDED_STUDENT_ROOM_LIMIT = 2  # scripts/seed.py

db = SessionLocal()
try:
    room_id = db.query(Room).order_by(Room.id).first().id
    other_room_id = db.query(Room).order_by(Room.id).all()[1].id
    gpu_id = db.query(GPUCluster).order_by(GPUCluster.id).first().id
    reservations_before = db.query(Reservation).count()

    # **Room quota lifted for the duration of this gate, and restored in
    # cleanup.** Deadline 6 capped STUDENT room holds at 2. This script
    # tests intervals, the BLOCKED gate and the exclusion constraint --
    # none of which is about quota -- and it books roughly ten slots on
    # one account to do it. Without this, eleven assertions fail with
    # QUOTA_EXCEEDED where an interval result was expected: the right
    # status code for entirely the wrong reason, which is the failure mode
    # this project keeps catching in its own tests.
    #
    # Lifted rather than worked around by spreading bookings across users:
    # several assertions below are specifically about ONE caller booking
    # adjacent and overlapping slots, and rewriting them to use different
    # callers would quietly change what they prove. The quota is tested
    # in `check_quotas.py`, which is where it belongs.
    # Restored to the SEEDED CONSTANT, not to whatever was found here. A
    # run that crashes between this line and cleanup leaves the quota
    # unlimited; capturing the current value would then record `None` as
    # "the seeded limit" and the next run would restore the corruption.
    # Learned by doing exactly that.
    _student_room_quota = (
        db.query(RoleQuota)
        .filter(
            RoleQuota.role == Role.STUDENT,
            RoleQuota.resource_type == ResourceType.ROOM,
        )
        .one()
    )
    _student_room_quota.max_units = None  # unlimited, for this script only
    db.commit()
finally:
    db.close()

created_ids: list[int] = []

# --- the boundary is still the boundary ---------------------------------
r = httpx.post(
    f"{BASE}/rooms/{room_id}/reservations",
    json={"start_time": at(10), "end_time": at(12)},
)
check("no token -> 401", r.status_code == 401, str(r.status_code))

# --- the happy path -----------------------------------------------------
# Any authenticated role may reserve a room, so a STUDENT token is the
# right one here. If this ever needs an admin, the role matrix has moved.
r = book(student, room_id, 10, 12)
check("student books [10,12) -> 201", r.status_code == 201, str(r.status_code))
first = r.json() if r.status_code == 201 else {}
if first.get("id"):
    created_ids.append(first["id"])
check("hold is ACTIVE", first.get("status") == "ACTIVE", str(first.get("status")))
check("hold names the caller", bool(first.get("user_id")), str(first.get("user_id")))

# --- THE CHECKPOINT: overlap is refused ---------------------------------
r = book(student, room_id, 11, 13)
check(
    "THE CHECKPOINT: overlapping [11,13) -> 409",
    r.status_code == 409,
    str(r.status_code),
)
check(
    "409 carries INTERVAL_CONFLICT",
    code_of(r) == "INTERVAL_CONFLICT",
    str(code_of(r)),
)

# Containment in both directions, and an identical window. All three are
# overlaps; a naive `start >= booked_end or end <= booked_start` written
# with one comparison inverted passes the case above and fails these.
for label, (s_, e_) in {
    "inner [10:30,11:30)": (10, 11),
    "enclosing [9,13)": (9, 13),
    "identical [10,12)": (10, 12),
}.items():
    r = book(student, room_id, s_, e_)
    check(f"overlap {label} -> 409", r.status_code == 409, str(r.status_code))

# --- adjacency must SUCCEED --------------------------------------------
# The whole reason the range is '[)' rather than '[]'. With inclusive
# bounds, back-to-back bookings would be impossible and this returns 409.
r = book(student, room_id, 12, 14)
check("adjacent [12,14) -> 201", r.status_code == 201, str(r.status_code))
if r.status_code == 201:
    created_ids.append(r.json()["id"])

r = book(student, room_id, 8, 10)
check("adjacent-before [8,10) -> 201", r.status_code == 201, str(r.status_code))
if r.status_code == 201:
    created_ids.append(r.json()["id"])

# --- the constraint is per room, not global -----------------------------
r = book(student, other_room_id, 10, 12)
check(
    "same slot, different room -> 201",
    r.status_code == 201,
    str(r.status_code),
)
if r.status_code == 201:
    created_ids.append(r.json()["id"])

# --- CONCURRENCY: the only case the constraint is actually needed for ---
# Everything above passes against a plain "is this slot free?" SELECT
# followed by an INSERT -- which is exactly the implementation this
# project rejects, and which would be indistinguishable from a correct one
# until two requests arrived together. N identical bookings released on a
# barrier: exactly one may commit, and the assertion that matters is the
# ROW COUNT, not the status codes, because status codes are what the
# server said.
#
# **Every racer is a DIFFERENT student, as of Deadline 6.** They shared
# the seed account until `reserve_room` took the user-row lock for the
# room quota; from that moment one account would have serialized all
# eight on the user row and fed them to the constraint one at a time. The
# assertion would still have passed and would have stopped measuring the
# thing it names. See `make_students`.
RACERS = 8
_racer_tokens = make_students(RACERS)
_barrier = threading.Barrier(RACERS)
_results: list[tuple[int, str | None]] = []
_lock = threading.Lock()


def _race(token: str) -> None:
    _barrier.wait()
    resp = book(token, room_id, 20, 22)
    with _lock:
        _results.append((resp.status_code, code_of(resp)))


_threads = [threading.Thread(target=_race, args=(t,)) for t in _racer_tokens]
for _t in _threads:
    _t.start()
for _t in _threads:
    _t.join()

tally = collections.Counter(_results)
check(
    f"{RACERS} simultaneous identical bookings -> exactly one 201",
    tally[(201, None)] == 1,
    str(dict(tally)),
)
check(
    f"the other {RACERS - 1} -> 409 INTERVAL_CONFLICT, no 500s",
    tally[(409, "INTERVAL_CONFLICT")] == RACERS - 1,
    str(dict(tally)),
)

db = SessionLocal()
try:
    landed = (
        db.query(Reservation)
        .filter(
            Reservation.resource_id == room_id,
            Reservation.status == ReservationStatus.ACTIVE,
            Reservation.start_time == DAY + dt.timedelta(hours=20),
        )
        .all()
    )
    created_ids.extend(row.id for row in landed)
finally:
    db.close()
check("exactly one row landed in the database", len(landed) == 1, f"{len(landed)} rows")

# --- a cancelled hold must not block its old slot -----------------------
# The constraint is partial on status = 'ACTIVE'. If that WHERE clause
# were ever dropped, releasing a room would leave the slot permanently
# unbookable -- and nothing else in this file would notice.
db = SessionLocal()
try:
    row = db.get(Reservation, created_ids[0])
    row.status = ReservationStatus.CANCELLED
    db.commit()
finally:
    db.close()

r = book(student, room_id, 10, 12)
check(
    "the slot of a CANCELLED hold is bookable again",
    r.status_code == 201,
    str(r.status_code),
)
if r.status_code == 201:
    created_ids.append(r.json()["id"])

# --- what the DATABASE does not enforce: the resource_type check --------
# `reservations.resource_id` is a foreign key to `resources`, and a GPU
# cluster IS a resource -- so the FK accepts this row happily. Only the
# service layer stands between a "room booking" and a GPU cluster. Read
# paths get this free from the polymorphic discriminator; write paths do
# not, which is precisely why it is asserted.
r = book(student, gpu_id, 10, 12)
check(
    "booking a GPU cluster through /rooms -> 404",
    r.status_code == 404,
    str(r.status_code),
)
r = book(student, 999999, 10, 12)
check("booking a nonexistent room -> 404", r.status_code == 404, str(r.status_code))

db = SessionLocal()
try:
    stray = db.query(Reservation).filter(Reservation.resource_id == gpu_id).count()
finally:
    db.close()
check("no reservation row against the GPU cluster", stray == 0, f"{stray} rows")

# --- what the DATABASE does not enforce: the BLOCKED gate ---------------
# Outstanding item 6, ratified at Deadline 3. The GiST constraint is
# partial on the RESERVATION's status, not the RESOURCE's, so the database
# is entirely indifferent to this. The gate is the whole guarantee.
# Flipped directly in the database because PATCH /rooms/{id} is Deadline 6.
db = SessionLocal()
try:
    room = db.get(Room, other_room_id)
    room.status = ResourceStatus.BLOCKED
    db.commit()
finally:
    db.close()

r = book(student, other_room_id, 30, 32)
check("booking a BLOCKED room -> 409", r.status_code == 409, str(r.status_code))
check("409 carries RESOURCE_BLOCKED", code_of(r) == "RESOURCE_BLOCKED", str(code_of(r)))

# Ratified semantics: blocking stops NEW allocations and does NOT evict
# existing ones -- the same rule as the capacity reduction in
# ARCHITECTURE_AND_WORKFLOWS.md section 13.
db = SessionLocal()
try:
    survivors = (
        db.query(Reservation)
        .filter(
            Reservation.resource_id == other_room_id,
            Reservation.status == ReservationStatus.ACTIVE,
        )
        .count()
    )
finally:
    db.close()
check(
    "blocking did NOT evict the existing hold",
    survivors == 1,
    f"{survivors} active holds",
)

# An ADMIN is refused too. BLOCKED is a fact about the resource, not a
# permission -- if this ever returns 201 for an admin, the gate has been
# confused with authorization.
r = book(admin, other_room_id, 30, 32)
check("admin is refused on a BLOCKED room too", r.status_code == 409, str(r.status_code))

db = SessionLocal()
try:
    room = db.get(Room, other_room_id)
    room.status = ResourceStatus.AVAILABLE
    db.commit()
finally:
    db.close()

# --- validation ---------------------------------------------------------
r = book(student, room_id, 12, 12)
check("empty window (start == end) -> 422", r.status_code == 422, str(r.status_code))
r = book(student, room_id, 14, 12)
check("inverted window -> 422", r.status_code == 422, str(r.status_code))
r = httpx.post(
    f"{BASE}/rooms/{room_id}/reservations", json={}, headers=bearer(student)
)
check("missing body fields -> 422", r.status_code == 422, str(r.status_code))

# A naive datetime is interpreted as UTC rather than rejected or left to
# the session TimeZone. Booked at 40:00 UTC-equivalent, then re-booked
# with an explicit +00:00 -- the second must conflict, which is only true
# if the first was stored as UTC.
naive = (DAY + dt.timedelta(hours=40)).replace(tzinfo=None).isoformat()
naive_end = (DAY + dt.timedelta(hours=42)).replace(tzinfo=None).isoformat()
r = httpx.post(
    f"{BASE}/rooms/{room_id}/reservations",
    json={"start_time": naive, "end_time": naive_end},
    headers=bearer(student),
)
check("naive datetime accepted -> 201", r.status_code == 201, str(r.status_code))
if r.status_code == 201:
    created_ids.append(r.json()["id"])
r = book(student, room_id, 40, 42)
check(
    "naive input was stored as UTC (the tz-aware repeat conflicts)",
    r.status_code == 409,
    str(r.status_code),
)

# --- the read endpoint agrees with the constraint -----------------------
# `GET /availability` reporting a slot free that the constraint then
# rejects would look like a concurrency bug and be neither.
r = httpx.get(
    f"{BASE}/rooms/{room_id}/availability",
    params={"start": at(20), "end": at(22)},
    headers=bearer(student),
)
check(
    "availability says a booked slot is NOT available",
    r.status_code == 200 and r.json()["available"] is False,
    str(r.json().get("available") if r.status_code == 200 else r.status_code),
)
r = httpx.get(
    f"{BASE}/rooms/{room_id}/availability",
    params={"start": at(100), "end": at(102)},
    headers=bearer(student),
)
check(
    "availability says a free slot IS available",
    r.status_code == 200 and r.json()["available"] is True,
    str(r.json().get("available") if r.status_code == 200 else r.status_code),
)

# --- the SHARE lock does what the gate needs, and no more ---------------
# Two claims, and the gate is only sound if BOTH hold:
#   1. an admin's write to the resource row CANNOT land between the status
#      check and the INSERT -- otherwise the booking lands on a room that
#      is blocked by the time it commits;
#   2. another booker is NOT blocked -- otherwise the application lock,
#      not the exclusion constraint, is what decides a concurrent slot
#      race, and the design claim in reserve_room's docstring is false.
# FOR UPDATE would satisfy (1) and break (2). Asserted here by holding the
# lock in one session and racing a second one with a short lock_timeout,
# because "it should block" and "it does block" are different statements.
locker = SessionLocal()
prober = SessionLocal()
try:
    locker.execute(
        text("SELECT id FROM resources WHERE id = :i FOR SHARE"), {"i": room_id}
    )

    prober.execute(text("SET lock_timeout = '750ms'"))
    try:
        prober.execute(
            text("UPDATE resources SET status = 'BLOCKED' WHERE id = :i"),
            {"i": room_id},
        )
        check("SHARE lock blocks an admin write to the row", False, "UPDATE succeeded")
    except OperationalError as exc:
        check(
            "SHARE lock blocks an admin write to the row",
            "LockNotAvailable" in type(exc.orig).__name__ or "lock timeout" in str(exc),
            type(exc.orig).__name__,
        )
    finally:
        prober.rollback()

    prober.execute(text("SET lock_timeout = '750ms'"))
    try:
        prober.execute(
            text("SELECT id FROM resources WHERE id = :i FOR SHARE"), {"i": room_id}
        )
        check("SHARE lock does NOT block another booker", True, "acquired")
    except OperationalError as exc:
        check("SHARE lock does NOT block another booker", False, type(exc.orig).__name__)
    finally:
        prober.rollback()
finally:
    locker.rollback()
    locker.close()
    prober.close()

# --- cleanup ------------------------------------------------------------
db = SessionLocal()
try:
    db.query(Reservation).filter(Reservation.id.in_(set(created_ids))).delete(
        synchronize_session=False
    )

    # The racers' holds are not in `created_ids` -- only one of the eight
    # committed and the script never learns which. Delete by owner.
    if made_users:
        db.query(Reservation).filter(
            Reservation.user_id.in_(made_users)
        ).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(made_users)).delete(
            synchronize_session=False
        )

    # Restore the room quota this script lifted. A gate that leaves policy
    # changed is a gate that breaks the next one.
    db.query(RoleQuota).filter(
        RoleQuota.role == Role.STUDENT,
        RoleQuota.resource_type == ResourceType.ROOM,
    ).one().max_units = SEEDED_STUDENT_ROOM_LIMIT

    db.commit()
    check(
        "room quota restored to its seeded value",
        db.query(RoleQuota)
        .filter(
            RoleQuota.role == Role.STUDENT,
            RoleQuota.resource_type == ResourceType.ROOM,
        )
        .one()
        .max_units
        == SEEDED_STUDENT_ROOM_LIMIT,
        str(SEEDED_STUDENT_ROOM_LIMIT),
    )
    check("racer accounts removed", db.query(User).count() == 3, str(db.query(User).count()))
    after = db.query(Reservation).count()
    check(
        "reservations back to post-seed count",
        after == reservations_before,
        f"{reservations_before} -> {after}",
    )
    check(
        "no room left BLOCKED",
        db.query(Room).filter(Room.status == ResourceStatus.BLOCKED).count() == 0,
        "",
    )
finally:
    db.close()

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
