from pydantic import BaseModel, ConfigDict

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
