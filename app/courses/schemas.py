from pydantic import BaseModel, ConfigDict, computed_field


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
