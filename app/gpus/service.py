from sqlalchemy.orm import Session

from app.gpus.schemas import GPUClusterCreate
from app.models.resource import GPUCluster


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
