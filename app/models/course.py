from sqlalchemy import CheckConstraint, String, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class Course(Base):
    """The catalogue entry — stable across semesters. Deliberately holds
    no capacity: seats are a property of a specific offering, not of the
    course, so two sections of the same course can differ.
    """
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class CourseOffering(Base):
    """One section in one semester. `enrolled_count` is THE counter locked
    and mutated by course registration — same pattern as
    `GPUCluster.allocated`: the row lock makes it correct, the CHECK makes
    a locking bug fail loudly instead of overselling seats.
    """
    __tablename__ = "course_offerings"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), nullable=False)
    instructor_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    semester: Mapped[str] = mapped_column(String(20), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[str] = mapped_column(String(5), nullable=False)   # "HH:MM"
    end_time: Mapped[str] = mapped_column(String(5), nullable=False)     # "HH:MM"
    days: Mapped[str] = mapped_column(String(20), nullable=False)        # e.g. "MWF"
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    enrolled_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("capacity > 0", name="offering_capacity_positive"),
        CheckConstraint(
            "enrolled_count >= 0 AND enrolled_count <= capacity",
            name="offering_enrollment_sane",
        ),
    )
