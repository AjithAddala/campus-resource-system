from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user, require_role
from app.database.session import get_db
from app.gpus import service
from app.gpus.schemas import GPUAvailability, GPUClusterCreate, GPUClusterRead
from app.models.enums import Role
from app.models.user import User

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
