from datetime import datetime

from sqlalchemy import Integer, Enum, ForeignKey, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.enums import ReservationStatus


class Reservation(Base):
    """Room reservations — interval-based. The GiST exclusion constraint
    (no overlap per resource_id where status=ACTIVE) is added BY HAND in
    the Alembic migration; autogenerate cannot produce it.
    """
    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, name="reservation_status_enum"),
        nullable=False,
        default=ReservationStatus.ACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)


class GPUReservation(Base):
    """GPU reservations — hold-until-release, NO start/end times.
    (Per DECISIONS.md: a scalar `allocated` counter can't represent
    interval capacity, and quota = concurrently-held units.)
    Quota = SUM(gpu_count) WHERE user_id=? AND status=ACTIVE.
    """
    __tablename__ = "gpu_reservations"

    id: Mapped[int] = mapped_column(primary_key=True)
    gpu_cluster_id: Mapped[int] = mapped_column(ForeignKey("gpu_clusters.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, name="reservation_status_enum"),
        nullable=False,
        default=ReservationStatus.ACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)