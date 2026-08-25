from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import ReservationStatus, ResourceStatus


class RoomRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: ResourceStatus
    building: str
    capacity: int


class RoomUpdate(BaseModel):
    """Admin PATCH body. Deadline 6.

    Every field is optional and the service uses `exclude_unset`, so
    omitting one means "leave it alone" rather than "set it to null".
    That distinction is the difference between PATCH and PUT, and a model
    of `X | None = None` cannot express it on its own — the service has to
    ask which keys the caller actually sent.

    `status` is the field this endpoint exists for: blocking a room for
    maintenance. Deadlines 3 and 4 wrote the gates that READ this column
    under a row lock; this is the only place that writes it.

    Note what is absent: `capacity` and `building`. Both are editable in
    principle and neither is in the plan's Deadline 6 column, which names
    this endpoint as *"block a room for maintenance."* Adding them would
    be scope creep two deadlines before a freeze.
    """

    status: ResourceStatus | None = None


class ReservationWindow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    start_time: datetime
    end_time: datetime


class RoomAvailability(BaseModel):
    """Same caveat as GPU availability: a boundary read, not a guarantee.

    `available` can go false between this response and the caller's POST.
    Nothing here prevents a double booking — the GiST exclusion
    constraint does, at write time. This endpoint exists so a human can
    pick a slot, not so the service layer can skip the constraint.
    """

    room_id: int
    name: str
    status: ResourceStatus
    start: datetime
    end: datetime
    available: bool
    conflicts: list[ReservationWindow]


class RoomCreate(BaseModel):
    """Admin-only room creation.

    `capacity` here is seats in the room -- a physical fact, and nothing
    like `GPUCluster.gpu_count`. Rooms are allocated by INTERVAL, not by
    unit: the invariant is "no two active reservations overlap", enforced
    by the GiST exclusion constraint, so there is no counter to guard and
    no `allocated` field to omit. Worth stating, because the two
    resources sharing one base table invites the assumption that they
    share an allocation model, and they deliberately do not.
    """

    name: str = Field(min_length=1, max_length=255)
    building: str = Field(min_length=1, max_length=255)
    capacity: int = Field(ge=1)


class ReservationCreate(BaseModel):
    """A room hold: an interval, and nothing else.

    No `resource_id` field -- the room is in the path. A body that could
    disagree with the URL is a body that will, and the path is the half
    the router already used to find and lock the row.

    **A naive datetime is interpreted as UTC, explicitly.** Postgres would
    otherwise resolve it against the session's `TimeZone`, so the stored
    instant would depend on who ran the request and with what settings --
    the same trap revision 1ca8b85b7626 hit converting `timestamp` to
    `timestamptz`, where the fix was an explicit `USING ... AT TIME ZONE
    'UTC'`. Rejecting naive input outright was the alternative; making the
    interpretation explicit costs one validator and blocks nobody.
    """

    model_config = ConfigDict(from_attributes=True)

    start_time: datetime
    end_time: datetime

    @field_validator("start_time", "end_time")
    @classmethod
    def _assume_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @model_validator(mode="after")
    def _ordered(self) -> "ReservationCreate":
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be before end_time")
        return self


class ReservationRead(BaseModel):
    """A confirmed hold. Unlike the availability schemas above, this one
    IS authoritative: it exists because a row was committed past the
    exclusion constraint.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    resource_id: int
    user_id: int
    start_time: datetime
    end_time: datetime
    status: ReservationStatus
    created_at: datetime
