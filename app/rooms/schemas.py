from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import ResourceStatus


class RoomRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: ResourceStatus
    building: str
    capacity: int


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
