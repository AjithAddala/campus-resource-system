from datetime import datetime

from sqlalchemy import Integer, Enum, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.enums import EnrollmentStatus


class Enrollment(Base):
    __tablename__ = "enrollments"
    __table_args__ = (
        UniqueConstraint("student_id", "course_offering_id", name="enrollment_unique"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    course_offering_id: Mapped[int] = mapped_column(ForeignKey("course_offerings.id"), nullable=False)
    status: Mapped[EnrollmentStatus] = mapped_column(
        Enum(EnrollmentStatus, name="enrollment_status_enum"),
        nullable=False,
        default=EnrollmentStatus.ACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)


class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    course_offering_id: Mapped[int] = mapped_column(ForeignKey("course_offerings.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)