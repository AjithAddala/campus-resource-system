from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.gpus.schemas import GPUClusterCreate, GPUReservationCreate, GPUReservationRead
from app.idempotency import service as idempotency
from app.models.enums import ReservationStatus, ResourceStatus, Role
from app.models.reservation import GPUReservation
from app.models.resource import GPUCluster
from app.models.user import User
from app.quotas import service as quotas

RESERVE_ENDPOINT = "gpu.reserve"


def list_clusters(db: Session) -> list[GPUCluster]:
    return db.query(GPUCluster).order_by(GPUCluster.id).all()


def get_cluster(db: Session, gpu_id: int) -> GPUCluster | None:
    """Returns None for an id that exists but is not a GPU cluster.

    Querying the subclass adds `resources.resource_type = 'GPU'` to the
    WHERE clause, because `resource_type` is the polymorphic
    discriminator. So `/gpus/3` where 3 is a room 404s rather than
    returning a half-populated row — the same resource_type check the
    room and GPU *write* paths have to make by hand later.
    """
    return db.query(GPUCluster).filter(GPUCluster.id == gpu_id).first()


def create_cluster(db: Session, payload: GPUClusterCreate) -> GPUCluster:
    """Create a cluster with zero units allocated.

    Constructing the subclass writes BOTH halves of the joined-table
    inheritance -- the `resources` row and the `gpu_clusters` row -- and
    sets `resource_type` from `polymorphic_identity`. Never hand-write
    the `resources` row; the same rule `scripts/seed.py` follows.

    Commits here rather than in the router, because `get_db()`
    deliberately does not commit: the boundary belongs on the line after
    the last write, where the statements it makes durable are visible.
    """
    cluster = GPUCluster(name=payload.name, gpu_count=payload.gpu_count, allocated=0)
    db.add(cluster)
    db.commit()
    db.refresh(cluster)
    return cluster


class ClusterBlocked(Exception):
    """The cluster is BLOCKED. Item 6 semantics, ratified at Deadline 3."""


class CapacityBelowAllocated(Exception):
    """An admin tried to shrink a cluster below what is currently held.

    See `update_cluster` for why this is a refusal rather than an
    eviction, and why it contradicts one sentence of
    `ARCHITECTURE_AND_WORKFLOWS.md` §13.
    """

    def __init__(self, requested: int, allocated: int):
        self.requested = requested
        self.allocated = allocated
        super().__init__(f"cannot set gpu_count={requested} below allocated={allocated}")


class CapacityExhausted(Exception):
    """Not enough free units on this cluster."""

    def __init__(self, total: int, allocated: int, requested: int):
        self.total = total
        self.allocated = allocated
        self.requested = requested
        super().__init__(f"{allocated}/{total} allocated, requested {requested}")


class NotOwner(Exception):
    """Caller is neither the owner of this reservation nor an ADMIN."""


def reserve_gpu(
    db: Session,
    gpu_id: int,
    user: User,
    payload: GPUReservationCreate,
    idempotency_key: str | None = None,
) -> GPUReservation | None:
    """THE transaction. Hold `gpu_count` units on a cluster until released.

    Returns the committed row, or None if `gpu_id` is not a GPU cluster.
    Raises `QuotaExceeded`, `ClusterBlocked` or `CapacityExhausted`, and
    from Deadline 5 also `KeyReplayed`, `KeyReuse` and `KeyInFlight`.

    `idempotency_key` is **optional, and that is a requirement rather than
    a convenience.** Benchmark 3 measures the difference between the two
    modes -- "no key -> 2 reservations; key -> 1 reservation, identical
    response" -- so the unkeyed path has to stay reachable or the
    benchmark has only one column. It is also the honest default: a caller
    who has not thought about retries gets today's behaviour, and a caller
    who has gets exactly-once by sending a header.

    ==================================================================
    LOCK ORDER -- USER ROW FIRST, RESOURCE ROW SECOND. NO EXCEPTIONS.
    ==================================================================
    Every path that takes both locks takes them in this order: allocation
    (here), cancellation (below), and course registration. If one path
    reversed them, two requests could hold one lock each and wait forever
    on the other. Because the order is global, a request holding the user
    lock cannot be waiting behind anyone holding the resource lock -- the
    cycle cannot form, so deadlock is structurally impossible rather than
    merely unlikely.

    The steps, and what each one protects:

      (1) exactly-once   insert the idempotency key HERE, above both
                         locks, so a replayed request does not even queue
                         for them.        <- keyed on the REQUEST
      (2) QUOTA GATE     lock the USER row, SUM held units, compare
                         against role policy.        <- keyed on the USER
      (3) CAPACITY GATE  lock the CLUSTER row, check status, compare
                         allocated against total.    <- keyed on the RESOURCE
      (4) write          counter and reservation row, then COMMIT.

    **Steps 2 and 3 guard different invariants and neither substitutes for
    the other.** The cluster lock cannot see a student holding units on a
    DIFFERENT cluster -- different row, no contention, both succeed, and
    the student ends up over quota with nothing overbooked. That failure
    is the whole argument of this project, and Benchmark 2 exists to show
    it: the lock was not missing, it was the wrong lock for that
    invariant.

    Everything from here to `db.commit()` is ONE transaction. No
    intermediate commit, so a failure at any gate rolls back every earlier
    statement, and both locks are held until commit -- which is why
    `get_db()` deliberately does not commit and `autocommit=False` is set
    in `session.py`. A commit we did not write would release a lock we
    thought we still held.
    """
    settings = get_settings()

    if get_cluster(db, gpu_id) is None:
        return None

    if idempotency_key is not None:
        idempotency.claim(
            db,
            idempotency_key,
            user.id,
            RESERVE_ENDPOINT,
            idempotency.request_fingerprint(
                {"gpu_id": gpu_id, "gpu_count": payload.gpu_count}
            ),
        )

    if not settings.BENCHMARK_UNSAFE_NO_USER_LOCK:
        db.execute(select(User.id).where(User.id == user.id).with_for_update())

    held = quotas.enforce_gpu_quota(db, user.id, user.role, payload.gpu_count)

    cluster = (
        db.query(GPUCluster)
        .filter(GPUCluster.id == gpu_id)
        .populate_existing()
        .with_for_update()
        .first()
    )

    if cluster is None:
        return None

    if cluster.status is ResourceStatus.BLOCKED:
        raise ClusterBlocked(gpu_id)

    if cluster.allocated + payload.gpu_count > cluster.gpu_count:
        raise CapacityExhausted(cluster.gpu_count, cluster.allocated, payload.gpu_count)

    cluster.allocated += payload.gpu_count
    reservation = GPUReservation(
        gpu_cluster_id=cluster.id,
        user_id=user.id,
        gpu_count=payload.gpu_count,
        status=ReservationStatus.ACTIVE,
    )
    db.add(reservation)

    if idempotency_key is not None:
        db.flush()
        idempotency.record_response(
            db,
            idempotency_key,
            user.id,
            GPUReservationRead.model_validate(reservation).model_dump(mode="json"),
            201,
        )

    db.commit()
    db.refresh(reservation)

    _ = held
    return reservation


def update_cluster(db: Session, gpu_id: int, payload) -> GPUCluster | None:
    """Admin edit of a cluster: `status` and/or `gpu_count`. None if not a cluster.

    Raises `CapacityBelowAllocated`.

    **Takes the cluster row FOR UPDATE**, unlike the room twin, and for
    the reason that separates those two paths everywhere else in this
    codebase: this one reads `allocated` and decides based on it. A
    boundary read would let an allocation commit between the check and the
    UPDATE, and the shrink would then land below what is held.

    ------------------------------------------------------------------
    THE CAPACITY-REDUCTION RULE CONTRADICTS `gpu_capacity_sane`.
    ------------------------------------------------------------------
    `ARCHITECTURE_AND_WORKFLOWS.md` §13 says: *"if an admin lowers a
    cluster from 8 to 4 while 6 are allocated, existing reservations are
    not retroactively evicted; the new limit applies only to future
    allocations and `allocated` drains naturally."*

    That cannot be implemented as written. `gpu_capacity_sane` is
    `allocated >= 0 AND allocated <= gpu_count`, so setting gpu_count=4
    with allocated=6 is rejected by Postgres — the transaction would fail
    with an IntegrityError and surface as a 500. The document describes a
    state the schema forbids.

    **Resolved by refusing the reduction (409), not by dropping the
    CHECK.** The constraint is what makes a locking bug in the flagship
    transaction fail loudly instead of quietly overselling the cluster;
    trading that for one sentence of documented convenience is a bad
    exchange. The admin can shrink to `allocated` immediately and further
    as holds are released, so §13's *intent* — never evict — is preserved
    exactly. Only its literal example is unavailable.

    Recorded as a decision rather than silently coded around: §13 needs
    the correction, and it is flagged in DECISIONS.md at Deadline 6.
    """
    cluster = (
        db.query(GPUCluster)
        .filter(GPUCluster.id == gpu_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if cluster is None:
        return None

    fields = payload.model_dump(exclude_unset=True)

    new_count = fields.get("gpu_count")
    if new_count is not None and new_count < cluster.allocated:
        raise CapacityBelowAllocated(new_count, cluster.allocated)

    for field, value in fields.items():
        setattr(cluster, field, value)

    db.commit()
    db.refresh(cluster)
    return cluster


def cancel_gpu_reservation(
    db: Session, gpu_id: int, reservation_id: int, user: User
) -> GPUReservation | None:
    """Release a hold. Owner or ADMIN only.

    Owned by A even though `reservations/` is otherwise B's, because it is
    a locking transaction and **must obey the same lock order as
    allocation**: user row, then cluster row. Reversing it in this one
    file would reintroduce the deadlock cycle across the whole
    application. Settled at Deadline 1 to avoid a Deadline 7 merge
    conflict.

    **Naturally idempotent.** Cancelling an already-CANCELLED hold is a
    no-op returning the same row, because the counter is only decremented
    on the ACTIVE -> CANCELLED transition. That is a direct benefit of
    recomputing held units by SUM rather than keeping a denormalized
    counter: with a counter, a repeated cancel would double-decrement and
    would need a guard of its own.

    Note the user lock is taken on the RESERVATION'S OWNER, not on the
    caller. An ADMIN cancelling someone else's hold must serialize against
    that user's allocations; locking the admin's own row would protect
    nothing.
    """
    reservation = db.get(GPUReservation, reservation_id)

    if reservation is None or reservation.gpu_cluster_id != gpu_id:
        return None

    if reservation.user_id != user.id and user.role is not Role.ADMIN:
        raise NotOwner(reservation_id)

    if reservation.status is ReservationStatus.CANCELLED:
        return reservation

    db.execute(select(User.id).where(User.id == reservation.user_id).with_for_update())
    cluster = (
        db.query(GPUCluster)
        .filter(GPUCluster.id == reservation.gpu_cluster_id)
        .populate_existing()
        .with_for_update()
        .first()
    )

    db.refresh(reservation)
    if reservation.status is ReservationStatus.CANCELLED:
        return reservation

    reservation.status = ReservationStatus.CANCELLED
    cluster.allocated -= reservation.gpu_count
    db.commit()
    db.refresh(reservation)
    return reservation
