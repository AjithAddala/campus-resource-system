from sqlalchemy.orm import Session

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
