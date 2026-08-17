from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.enums import EnrollmentStatus


class Enrollment(Base):
    __tablename__ = "enrollments"
    __table_args__ = (
        UniqueConstraint("student_id", "course_offering_id", name="enrollment_unique"),
        # enrollment_unique leads with student_id, so it serves the
        # course-load quota but NOT offering-keyed reads: the class roster
        # and the enrolled_count reconciliation query.
        Index("ix_enrollments_offering_status", "course_offering_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    course_offering_id: Mapped[int] = mapped_column(ForeignKey("course_offerings.id"), nullable=False)
    status: Mapped[EnrollmentStatus] = mapped_column(
        Enum(EnrollmentStatus, name="enrollment_status_enum"),
        nullable=False,
        default=EnrollmentStatus.ACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WaitlistEntry(Base):
    """FIFO order is `created_at` — there is no stored position. Promotion
    reads the oldest entry for the offering; nothing is renumbered, so a
    promotion touches exactly one row.
    """
    __tablename__ = "waitlist_entries"
    __table_args__ = (
        UniqueConstraint("student_id", "course_offering_id", name="waitlist_unique"),
        # Serves the promotion query: WHERE course_offering_id = ?
        # ORDER BY created_at, id LIMIT 1 — run while holding the offering lock.
        Index(
            "ix_waitlist_entries_offering_created",
            "course_offering_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    course_offering_id: Mapped[int] = mapped_column(ForeignKey("course_offerings.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )