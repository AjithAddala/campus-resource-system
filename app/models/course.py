from sqlalchemy import String, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)


class CourseOffering(Base):
    __tablename__ = "course_offerings"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), nullable=False)
    semester: Mapped[str] = mapped_column(String(20), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[str] = mapped_column(String(5), nullable=False)   # "HH:MM"
    end_time: Mapped[str] = mapped_column(String(5), nullable=False)     # "HH:MM"
    days: Mapped[str] = mapped_column(String(20), nullable=False)        # e.g. "MWF"