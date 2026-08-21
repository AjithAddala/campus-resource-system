from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ResourceStatus


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
