"""Per-role allocation quotas — the second of the three guarantees.

**This is a helper called from inside other modules' transactions, and it
never opens one of its own.** That is what makes the guarantee atomic:
the quota gate and the write it guards must commit or roll back together,
so this module takes a `Session` it did not create, emits no COMMIT, and
returns plain values or raises plain exceptions. It knows nothing about
HTTP.

The invariant it enforces is a fact about the **user**, not about any
resource:

    a user never holds more concurrent units than their role permits

which is why the caller must already hold the **user row** lock when it
calls `held_gpu_units`. Nothing here takes that lock -- a helper that
locked on its own would hide the project's most important line inside a
utility function, where nobody reading the transaction would see it.

Why a SUM rather than a counter column: held units are recomputed from
`gpu_reservations` under the user lock, so they cannot drift out of sync
with reality. A denormalized counter is an optimization we have not
earned -- no benchmark has yet shown the user row is a bottleneck. See
DECISIONS.md, "Why the counter stays out".
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enrollment import Enrollment
from app.models.enums import (
    EnrollmentStatus,
    ReservationStatus,
    ResourceType,
    Role,
)
from app.models.quota import RoleQuota
from app.models.reservation import GPUReservation, Reservation


class QuotaExceeded(Exception):
    """The caller is at their personal limit for this resource type.

    Carries the numbers rather than a message, because the router turns
    them into prose and B's benchmarks assert on them. `limit` is what
    the policy allows, `held` is what they already have, `requested` is
    what they asked for.
    """

    def __init__(self, resource_type: ResourceType, limit: int, held: int, requested: int):
        self.resource_type = resource_type
        self.limit = limit
        self.held = held
        self.requested = requested
        super().__init__(
            f"{resource_type.value}: holds {held}, requested {requested}, limit {limit}"
        )


class QuotaNotConfigured(Exception):
    """No `RoleQuota` row exists for this (role, resource_type) pair.

    **Absent and NULL are two different things, and conflating them is the
    bug this exception exists to prevent.** `scripts/seed.py` deliberately
    omits `(FACULTY, COURSE)` -- course registration is STUDENT-only, so
    the pair is unreachable behind the 403 -- while `(ADMIN, *)` rows do
    exist with `max_units = NULL`, meaning unlimited.

    So: NULL is a policy that says yes. A missing row is *no policy at
    all*, and this **fails closed**. Treating it as unlimited would be the
    dangerous default -- one un-seeded row would silently switch off the
    invariant this project exists to enforce, and every test would still
    pass. It never fires on a correctly seeded system, and when it does
    fire it names the pair, which is the only debugging information that
    helps.
    """

    def __init__(self, role: Role, resource_type: ResourceType):
        self.role = role
        self.resource_type = resource_type
        super().__init__(f"no quota policy for ({role.value}, {resource_type.value})")


def limit_for(db: Session, role: Role, resource_type: ResourceType) -> int | None:
    """The policy for this pair: an int cap, or None meaning unlimited.

    Raises `QuotaNotConfigured` when no row exists -- see above; the two
    Nones are not the same None.

    Read WITHOUT a lock, deliberately. `role_quotas` is admin-editable
    policy, not per-user state: an admin raising a limit mid-transaction
    is not a correctness problem, because the invariant is "held <= limit"
    at commit time and the caller is holding the user row, so `held`
    cannot move underneath it. Locking policy rows would serialize every
    allocation in the system on a handful of rows for no invariant.
    """
    row = db.execute(
        select(RoleQuota).where(
            RoleQuota.role == role, RoleQuota.resource_type == resource_type
        )
    ).scalar_one_or_none()

    if row is None:
        raise QuotaNotConfigured(role, resource_type)

    return row.max_units


def held_gpu_units(db: Session, user_id: int) -> int:
    """GPU units this user currently holds. **Caller must hold the user row lock.**

    `COALESCE(SUM(...), 0)` -- a user with no reservations must read 0,
    not None, or the arithmetic below silently becomes a TypeError on the
    first allocation of every new account.

    Served by `ix_gpu_reservations_user_status (user_id, status)`, which
    exists precisely because this query runs inside the hottest
    transaction while holding the user lock: a sequential scan here is
    lock time paid by every other request from that user.
    """
    return db.execute(
        select(func.coalesce(func.sum(GPUReservation.gpu_count), 0)).where(
            GPUReservation.user_id == user_id,
            GPUReservation.status == ReservationStatus.ACTIVE,
        )
    ).scalar_one()


def enforce_gpu_quota(db: Session, user_id: int, role: Role, requested: int) -> int:
    """Raise `QuotaExceeded` if this request would breach the caller's cap.

    Returns the currently-held unit count, so the caller can report it.

    **Must be called with the user row already locked FOR UPDATE.** The
    read-then-write between `held_gpu_units` and the eventual INSERT is
    exactly the window this project exists to talk about: without the
    lock, one student firing two concurrent 2-unit requests at two
    DIFFERENT clusters has both of them read held=0, both pass a limit of
    2, and both commit -- and the student holds 4. Neither cluster was
    overbooked and both cluster locks worked perfectly. The lock was not
    missing; it was the *wrong lock for that invariant*.
    """
    limit = limit_for(db, role, ResourceType.GPU)
    held = held_gpu_units(db, user_id)

    if limit is not None and held + requested > limit:
        raise QuotaExceeded(ResourceType.GPU, limit, held, requested)

    return held


def held_room_reservations(db: Session, user_id: int) -> int:
    """ACTIVE room holds for this user. **Caller must hold the user lock.**

    A COUNT, not a SUM: one reservation is one hold.

    **Deliberately not time-aware, and this is worth stating because it
    looks like a bug.** A reservation whose `end_time` has passed but
    whose status is still ACTIVE counts against the quota. Nothing in this
    system expires reservations — there is no sweeper job, and
    `ReservationStatus` has exactly two values — so ACTIVE *is* the
    definition of held. Making the quota time-aware here would invent a
    third state that the exclusion constraint, the cancel path and the
    availability endpoint all know nothing about, and the four would drift
    apart. If expiry is ever added, this query changes with it and not
    before.

    Served by `ix_reservations_user_id`. That index leads with `user_id`
    and does not carry `status`, so Postgres filters the status after the
    index lookup — acceptable, because a user's total reservation count is
    small by construction (it is the thing being capped).
    """
    return db.execute(
        select(func.count(Reservation.id)).where(
            Reservation.user_id == user_id,
            Reservation.status == ReservationStatus.ACTIVE,
        )
    ).scalar_one()


def enforce_room_quota(db: Session, user_id: int, role: Role, requested: int = 1) -> int:
    """Raise `QuotaExceeded` if one more room hold would breach the cap.

    Returns the current count, so the caller can report it.

    **Must be called with the user row already locked FOR UPDATE**, and
    the reason is the same shape as the GPU argument rather than a
    restatement of it: two concurrent bookings of two DIFFERENT rooms
    contend on no common row. Each takes its own resource lock, each
    reads the same count, both pass, and the user ends up over quota with
    neither room double-booked. The exclusion constraint cannot see this
    — it is partial on one resource's interval, and these are two
    resources. Only the user lock serializes it.
    """
    limit = limit_for(db, role, ResourceType.ROOM)
    held = held_room_reservations(db, user_id)

    if limit is not None and held + requested > limit:
        raise QuotaExceeded(ResourceType.ROOM, limit, held, requested)

    return held


def held_course_enrollments(db: Session, user_id: int) -> int:
    """ACTIVE enrollments for this user. **Caller must hold the user lock.**

    A COUNT, and it counts `EnrollmentStatus.ACTIVE` only — DROPPED rows
    survive forever because `enrollment_unique` is unconditional (a
    student who dropped still owns a row, which is what makes
    re-registration an UPDATE). Counting anything but ACTIVE would make a
    student's course load permanently ratchet upward as they dropped
    courses, which is the opposite of what a load limit means.

    `WAITLISTED` is not counted either, and that is a live question rather
    than a settled one: outstanding item 7 asks whether that enum value is
    ever used, since waitlist entries live in their own table. If item 7
    resolves toward writing WAITLISTED enrollment rows, this query is one
    of the places that has to be revisited — a seat you are queuing for
    is not a seat you hold.

    Served by `enrollment_unique (student_id, course_offering_id)`, whose
    leading column is `student_id`.
    """
    return db.execute(
        select(func.count(Enrollment.id)).where(
            Enrollment.student_id == user_id,
            Enrollment.status == EnrollmentStatus.ACTIVE,
        )
    ).scalar_one()


def enforce_course_quota(
    db: Session, user_id: int, role: Role, requested: int = 1
) -> int:
    """Raise `QuotaExceeded` if one more enrollment would breach the cap.

    **Must be called with the user row already locked FOR UPDATE.** That
    lock is already held by the time this runs: `courses.service.register`
    took it at Deadline 4 for the schedule-overlap check, which is a fact
    about the student for exactly the same reason this is. So Deadline 6
    adds a gate inside a lock that already exists — an addition, not a
    reordering, which is what the Deadline 4 write-up predicted.
    """
    limit = limit_for(db, role, ResourceType.COURSE)
    held = held_course_enrollments(db, user_id)

    if limit is not None and held + requested > limit:
        raise QuotaExceeded(ResourceType.COURSE, limit, held, requested)

    return held


def usage_snapshot(db: Session, user_id: int, role: Role) -> list[dict]:
    """Limits and current usage for all three resource types.

    **Takes NO lock, deliberately, and this is the one place in the module
    where that is a decision rather than a rule.** Every other reader here
    is inside a transaction that already holds the user row. This one is
    an HTTP GET: a caller asking "what am I allowed?" is shown a number
    that may be stale by the time they act on it, and that is fine —
    the allocation transaction is what has to be right, and it re-reads
    everything under the lock. Locking the user row to render a dashboard
    would put a read endpoint in contention with the flagship write path,
    which is a real cost paid for an illusion of freshness.

    **Missing policy is reported, not raised.** `limit_for` fails closed
    with `QuotaNotConfigured`, which is correct inside an allocation — no
    policy means no permission. It is wrong here. A caller asking what
    their limits are should not get an error because one (role, resource)
    pair is unseeded; `(FACULTY, COURSE)` is deliberately absent, so a
    faculty member calling this endpoint would otherwise get a 409 for
    asking a reasonable question. Each row carries `configured` instead,
    and the unconfigured one reports its usage with a null limit.
    """
    counters = {
        ResourceType.GPU: held_gpu_units,
        ResourceType.ROOM: held_room_reservations,
        ResourceType.COURSE: held_course_enrollments,
    }

    rows = []
    for resource_type, count_held in counters.items():
        try:
            limit = limit_for(db, role, resource_type)
            configured = True
        except QuotaNotConfigured:
            limit = None
            configured = False

        rows.append(
            {
                "resource_type": resource_type,
                "limit": limit,
                "held": count_held(db, user_id),
                "unlimited": configured and limit is None,
                "configured": configured,
            }
        )

    return rows
