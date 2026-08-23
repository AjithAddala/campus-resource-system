from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user, require_role
from app.core.errors import coded_error
from app.database.session import get_db
from app.gpus import service
from app.gpus.schemas import (
    GPUAvailability,
    GPUClusterCreate,
    GPUClusterRead,
    GPUReservationCreate,
    GPUReservationRead,
)
from app.models.enums import Role
from app.models.user import User
from app.quotas import service as quotas

router = APIRouter(prefix="/gpus", tags=["gpus"])


@router.post("", response_model=GPUClusterRead, status_code=status.HTTP_201_CREATED)
def create_gpu(
    payload: GPUClusterCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_role(Role.ADMIN)),
) -> GPUClusterRead:
    """Create a GPU cluster. ADMIN only.

    **This is the route Deadline 3's checkpoint names** -- *"student token
    on POST /gpus returns 403"* -- and until now it did not exist, so the
    checkpoint had nothing to fire against. Creating a resource is
    [ADMIN] in the role matrix of both INIT_PLAN.md section 13 and
    ARCHITECTURE_AND_WORKFLOWS.md section 8, but like `GET /me` and the admin
    PATCH endpoints it was never assigned a deadline. It is assigned
    here, because a `require_role` with no route to guard is a dependency
    nobody has proved fires.

    `require_role` is a **dependency**, not an `if` at the top of this
    function. That is the whole point: FastAPI resolves it before the
    body runs, so a student's 403 cannot leave a half-created cluster
    behind. `scripts/check_rbac.py` asserts that by counting rows after
    the rejection rather than by trusting the status code -- the same
    argument as Deadline 2's duplicate-registration audit, where the
    status code was what the server said and the row count was what
    happened.
    """
    return service.create_cluster(db, payload)


@router.get("", response_model=list[GPUClusterRead])
def list_gpus(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[GPUClusterRead]:
    return service.list_clusters(db)


@router.get("/{gpu_id}", response_model=GPUClusterRead)
def get_gpu(
    gpu_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> GPUClusterRead:
    cluster = service.get_cluster(db, gpu_id)
    if cluster is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "GPU cluster not found")
    return cluster


@router.get("/{gpu_id}/availability", response_model=GPUAvailability)
def get_gpu_availability(
    gpu_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> GPUAvailability:
    cluster = service.get_cluster(db, gpu_id)
    if cluster is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "GPU cluster not found")
    # `free` is computed from the row as read, with no lock. `status` is
    # reported alongside rather than folded into `free`: whether BLOCKED
    # means "zero free" is outstanding item 6, still unratified, and a
    # read endpoint is the wrong place to invent the answer.
    return GPUAvailability(
        gpu_id=cluster.id,
        name=cluster.name,
        status=cluster.status,
        total=cluster.gpu_count,
        allocated=cluster.allocated,
        free=cluster.gpu_count - cluster.allocated,
    )


@router.post(
    "/{gpu_id}/reservations",
    response_model=GPUReservationRead,
    status_code=status.HTTP_201_CREATED,
)
def reserve_gpu(
    gpu_id: int,
    payload: GPUReservationCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> GPUReservationRead:
    """Hold GPU units on a cluster until released. Any authenticated role.

    **The flagship path.** It is the only endpoint where all three of the
    system's guarantees meet: exactly-once (Deadline 5, via an
    `Idempotency-Key` header), quota (the user row lock), and capacity
    (the cluster row lock). `service.reserve_gpu` carries the reasoning
    for the order they are taken in.

    Body is `{"gpu_count": N}` and carries **no times** -- holds run until
    cancelled. See DECISIONS.md for why timestamps and a scalar
    `allocated` counter cannot coexist.

    Three distinct 409s can come out of here, and a client can tell them
    apart without parsing prose, which is the entire reason
    `core/errors.py` exists:

        QUOTA_EXCEEDED      you hold too much     -> release something
        CAPACITY_EXHAUSTED  the cluster is full   -> wait, or try another
        RESOURCE_BLOCKED    admin blocked it      -> try another, do not wait
    """
    try:
        reservation = service.reserve_gpu(db, gpu_id, user, payload)
    except quotas.QuotaExceeded as exc:
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "QUOTA_EXCEEDED",
            f"You hold {exc.held} of {exc.limit} permitted GPU units; "
            f"requested {exc.requested}.",
        )
    except quotas.QuotaNotConfigured as exc:
        # Fails closed. No policy row for this (role, resource) pair means
        # no policy at all -- unlike max_units = NULL, which is a policy
        # that says unlimited. Treating a missing row as unlimited would
        # let one un-seeded row silently switch the quota system off, and
        # every test would still pass.
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "QUOTA_NOT_CONFIGURED",
            f"No quota policy is configured for role {exc.role.value} "
            f"and resource {exc.resource_type.value}.",
        )
    except service.ClusterBlocked:
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "RESOURCE_BLOCKED",
            "This cluster is blocked and is not accepting new reservations.",
        )
    except service.CapacityExhausted as exc:
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "CAPACITY_EXHAUSTED",
            f"Only {exc.total - exc.allocated} of {exc.total} units are free; "
            f"requested {exc.requested}.",
        )

    if reservation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "GPU cluster not found")

    return reservation


@router.delete("/{gpu_id}/reservations/{reservation_id}", response_model=GPUReservationRead)
def cancel_gpu_reservation(
    gpu_id: int,
    reservation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> GPUReservationRead:
    """Release a hold. Owner or ADMIN.

    **The path mirrors the POST above**, which is outstanding item 8
    ratified at Deadline 4. `DELETE /reservations/{id}` was ambiguous:
    room holds live in `reservations` and GPU holds in `gpu_reservations`,
    each with its own id sequence, so `/reservations/5` named a row in
    both tables. Scoping the cancel under its resource removes the
    ambiguity structurally rather than by documentation.

    Returns the cancelled row rather than 204, so the caller can see the
    status flip and the response has something to be idempotent *about* --
    cancelling twice returns the same body, not an error.

    The ownership check is here in spirit but enforced in the service,
    because it needs the reservation row: `require_role` cannot express
    "owner OR admin" from a token alone.
    """
    try:
        reservation = service.cancel_gpu_reservation(db, gpu_id, reservation_id, user)
    except service.NotOwner:
        # 403, not 404. The row exists and the caller is authenticated;
        # they simply may not touch it. Hiding it behind a 404 would be
        # defensible for a resource whose existence is secret, and a
        # reservation id is not.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden")

    if reservation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reservation not found")

    return reservation
