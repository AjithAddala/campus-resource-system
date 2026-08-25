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


class WaitlistEntryRead(BaseModel):
    """A place in the queue.

    `position` is **computed at read time and never stored** — there is no
    `position` column, dropped in revision `c86676652ca2`. It is derived
    by `ROW_NUMBER() OVER (ORDER BY created_at, id)` and is therefore a
    display value, not a handle: it changes when anyone ahead leaves or is
    promoted, and no caller should store it or branch on it.

    The `id` tiebreak in that ordering is load-bearing rather than
    cosmetic. `func.now()` is transaction start time, so entries written
    inside one transaction share a `created_at` exactly and `created_at`
    alone cannot express FIFO between them.

    No `status` field, unlike `EnrollmentRead`: a waitlist entry either
    exists or it does not. `enrollment_unique` forces a dropped student to
    keep a row, which is why that model needs a status to distinguish
    "has a row" from "is enrolled"; leaving a waitlist deletes the row, so
    the question does not arise.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    student_id: int
    course_offering_id: int
    created_at: datetime
    position: int
