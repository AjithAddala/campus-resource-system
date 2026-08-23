from datetime import datetime

from pydantic import BaseModel, ConfigDict, computed_field

from app.models.enums import EnrollmentStatus


class CourseRead(BaseModel):
    """The catalogue entry. Deliberately carries no capacity — seats
    belong to an offering (revision 268c10da1da4), so there is nothing
    seat-shaped to report at this level.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str


class CourseOfferingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    course_id: int
    instructor_id: int
    semester: str
    year: int
    start_time: str  # "HH:MM", zero-padded
    end_time: str
    days: str
    capacity: int
    enrolled_count: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def seats_available(self) -> int:
        """Derived for display only.

        Registration must never trust this number: it is read without a
        lock, and the seat gate is `enrolled_count < capacity` evaluated
        under `SELECT ... FOR UPDATE` on the offering row.
        """
        return self.capacity - self.enrolled_count


class EnrollmentRead(BaseModel):
    """A seat, held or released.

    Carries `status` rather than only existing/not existing, because
    `enrollment_unique` is unconditional: a dropped student still owns a
    row, so "has a row" and "is enrolled" are different questions.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    student_id: int
    course_offering_id: int
    status: EnrollmentStatus
    created_at: datetime
