from sqlalchemy import CheckConstraint, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.enums import ResourceStatus, ResourceType


class Resource(Base):
    """Base table for anything bookable. Joined-table inheritance:
    `rooms` and `gpu_clusters` share this table's id, so a Reservation
    can point at a resource without knowing which kind it is.
    """
    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_type: Mapped[ResourceType] = mapped_column(
        Enum(ResourceType, name="resource_type_enum"), nullable=False, index=True
    )
    status: Mapped[ResourceStatus] = mapped_column(
        Enum(ResourceStatus, name="resource_status_enum"),
        nullable=False,
        default=ResourceStatus.AVAILABLE,
    )

    __mapper_args__ = {
        "polymorphic_identity": ResourceType.COURSE,
        "polymorphic_on": "resource_type",
    }


class Room(Resource):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(ForeignKey("resources.id"), primary_key=True)
    building: Mapped[str] = mapped_column(String(255), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"polymorphic_identity": ResourceType.ROOM}

    __table_args__ = (
        CheckConstraint("capacity > 0", name="room_capacity_positive"),
    )


class GPUCluster(Resource):
    """`allocated` is THE counter locked and mutated by reserve_gpu().
    The CHECK is a safety net, not the mechanism — the row lock is what
    makes it correct; the constraint makes a locking bug fail loudly.
    """
    __tablename__ = "gpu_clusters"

    id: Mapped[int] = mapped_column(ForeignKey("resources.id"), primary_key=True)
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False)
    allocated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __mapper_args__ = {"polymorphic_identity": ResourceType.GPU}

    __table_args__ = (
        CheckConstraint(
            "allocated >= 0 AND allocated <= gpu_count", name="gpu_capacity_sane"
        ),
    )