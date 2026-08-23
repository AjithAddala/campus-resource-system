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

from app.models.enums import ReservationStatus, ResourceType, Role
from app.models.quota import RoleQuota
from app.models.reservation import GPUReservation


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

    # None means unlimited (ADMIN). Note this is checked BEFORE the
    # comparison rather than defaulting to a large number: a sentinel like
    # 999999 would make "unlimited" a number that could be exceeded.
    if limit is not None and held + requested > limit:
        raise QuotaExceeded(ResourceType.GPU, limit, held, requested)

    return held
