import re
from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from app.models.enums import EnrollmentStatus

DAY_CODES = frozenset("MTWRFSU")

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

COURSE_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9 .-]*$")


class CourseCreate(BaseModel):
    """Admin POST body for a catalogue entry.

    Carries no capacity and no instructor, for the same reason `CourseRead`
    reports neither: both belong to an offering (revision
    `268c10da1da4`), and a course that claimed a seat count would be a
    second source of truth for a number the offering row already owns.
    """

    code: str = Field(min_length=2, max_length=50)
    name: str = Field(min_length=1, max_length=255)

    @field_validator("code")
    @classmethod
    def _canonical_code(cls, v: str) -> str:
        v = " ".join(v.split()).upper()
        if not COURSE_CODE_RE.match(v):
            raise ValueError(
                "code must be alphanumeric, optionally with spaces, dots or hyphens"
            )
        return v

    @field_validator("name")
    @classmethod
    def _tidy_name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("name must not be blank")
        return v


class CourseOfferingCreate(BaseModel):
    """Admin POST body for one section in one semester.

    Every field validated here is one the database would otherwise take on
    trust or reject with a message no caller can act on. `capacity > 0`
    duplicates the `offering_capacity_positive` CHECK **deliberately**:
    the CHECK is the guarantee, this is the error message. A caller who
    posts `capacity: 0` should get a 422 naming the field, not a 500
    carrying a constraint name.
    """

    course_id: int = Field(gt=0)
    instructor_id: int = Field(gt=0)
    semester: str = Field(min_length=1, max_length=20)
    year: int = Field(ge=2000, le=2100)
    start_time: str
    end_time: str
    days: str = Field(min_length=1, max_length=20)
    capacity: int = Field(gt=0)

    @field_validator("semester")
    @classmethod
    def _canonical_semester(cls, v: str) -> str:
        v = " ".join(v.split()).upper()
        if not v.isalpha():
            raise ValueError("semester must be alphabetic, e.g. AUTUMN")
        return v

    @field_validator("start_time", "end_time")
    @classmethod
    def _padded_time(cls, v: str) -> str:
        v = v.strip()
        if not TIME_RE.match(v):
            raise ValueError('time must be zero-padded 24-hour "HH:MM", e.g. "09:00"')
        return v

    @field_validator("days")
    @classmethod
    def _canonical_days(cls, v: str) -> str:
        v = v.strip().upper()
        unknown = sorted(set(v) - DAY_CODES)
        if unknown:
            raise ValueError(
                f"unknown day code(s) {''.join(unknown)!r}; "
                f"use single characters from {''.join(sorted(DAY_CODES))} "
                "(R = Thursday, U = Sunday)"
            )
        if len(set(v)) != len(v):
            raise ValueError("day codes must not repeat")
        return v

    @model_validator(mode="after")
    def _start_before_end(self) -> "CourseOfferingCreate":
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be earlier than end_time")
        return self


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
    start_time: str
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
