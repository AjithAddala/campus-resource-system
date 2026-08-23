from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ReservationStatus, ResourceStatus


class GPUClusterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: ResourceStatus
    gpu_count: int
    allocated: int


class GPUAvailability(BaseModel):
    """A boundary read, and deliberately not authoritative.

    `free` is stale the moment it is returned — another request may take
    the last unit before the caller acts on it. That is fine and is the
    design: the capacity guarantee lives in the allocation transaction,
    under `SELECT ... FOR UPDATE` on the cluster row. This endpoint is
    for showing a human what is there, never for deciding whether an
    allocation may proceed.
    """

    gpu_id: int
    name: str
    status: ResourceStatus
    total: int
    allocated: int
    free: int


class GPUClusterCreate(BaseModel):
    """Admin-only cluster creation.

    `allocated` is deliberately NOT a field. It is derived state owned by
    the allocation transaction, and letting a caller set it would mean a
    cluster could be born already over-allocated -- the exact invariant
    `gpu_capacity_sane` exists to protect. New clusters start at zero,
    always.
    """

    name: str = Field(min_length=1, max_length=255)
    gpu_count: int = Field(ge=1)


class GPUReservationCreate(BaseModel):
    """A GPU hold: a unit count, and nothing else.

    **No start/end times, deliberately** -- GPU holds are
    hold-until-release. The original design had both timestamps and a
    scalar `allocated` counter, and those do not compose: if reservations
    are time-bounded then capacity is a question about intervals, and a
    single integer cannot answer "at 3pm, how many are allocated?". See
    DECISIONS.md; do not re-add them.

    `ge=1` is enforced here rather than in the transaction, because a
    zero- or negative-unit request is a malformed body (422), not an
    allocation that lost a race (409). A negative count would otherwise
    reach the capacity gate and pass it, since `allocated + (-2)` is
    always under the limit.
    """

    gpu_count: int = Field(ge=1)


class GPUReservationRead(BaseModel):
    """A confirmed hold. Authoritative, unlike GPUAvailability: it exists
    because a row committed under the cluster lock.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    gpu_cluster_id: int
    user_id: int
    gpu_count: int
    status: ReservationStatus
    created_at: datetime
