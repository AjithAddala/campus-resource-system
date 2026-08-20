from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.database.session import get_db
from app.models.user import User
from app.rooms import service
from app.rooms.schemas import RoomAvailability, RoomRead

router = APIRouter(prefix="/rooms", tags=["rooms"])


@router.get("", response_model=list[RoomRead])
def list_rooms(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[RoomRead]:
    return service.list_rooms(db)


@router.get("/{room_id}", response_model=RoomRead)
def get_room(
    room_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> RoomRead:
    room = service.get_room(db, room_id)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")
    return room


@router.get("/{room_id}/availability", response_model=RoomAvailability)
def get_room_availability(
    room_id: int,
    start: datetime = Query(..., description="Window start, ISO 8601, tz-aware"),
    end: datetime = Query(..., description="Window end, ISO 8601, tz-aware"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> RoomAvailability:
    # Rejected here rather than in the service: an inverted or empty
    # window is a malformed request, not a domain outcome. Postgres would
    # also raise on tstzrange(start, end) with start > end, but as a 500.
    if start >= end:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "start must be before end"
        )

    room = service.get_room(db, room_id)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")

    conflicts = service.conflicting_reservations(db, room_id, start, end)
    return RoomAvailability(
        room_id=room.id,
        name=room.name,
        status=room.status,
        start=start,
        end=end,
        available=not conflicts,
        conflicts=conflicts,
    )
