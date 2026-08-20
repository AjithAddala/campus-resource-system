from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.database.session import get_db
from app.gpus import service
from app.gpus.schemas import GPUAvailability, GPUClusterRead
from app.models.user import User

router = APIRouter(prefix="/gpus", tags=["gpus"])


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
