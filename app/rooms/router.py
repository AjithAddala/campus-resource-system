from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user, require_role
from app.core.errors import coded_error
from app.database.session import get_db
from app.models.enums import Role
from app.models.user import User
from app.rooms import service
from app.rooms.schemas import (
    ReservationCreate,
    ReservationRead,
    RoomAvailability,
    RoomCreate,
    RoomRead,
)

router = APIRouter(prefix="/rooms", tags=["rooms"])


@router.post("", response_model=RoomRead, status_code=status.HTTP_201_CREATED)
def create_room(
    payload: RoomCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_role(Role.ADMIN)),
) -> RoomRead:
    """Create a room. ADMIN only.

    The GPU twin of this route is what Deadline 3's checkpoint fires at;
    this one exists for the same reason and carries the same gate. It
    also gives B's room-reservation work at Deadline 3 a way to make a
    room without reaching for the seed script or psql.
    """
    return service.create_room(db, payload)


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


@router.post(
    "/{room_id}/reservations",
    response_model=ReservationRead,
    status_code=status.HTTP_201_CREATED,
)
def reserve_room(
    room_id: int,
    payload: ReservationCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReservationRead:
    """Hold a room for an interval.

    **Any authenticated caller**, per the role matrix -- reserving a room
    is STUDENT, FACULTY and ADMIN alike, so this takes `get_current_user`
    and not `require_role`. The limit on how many a caller may hold is a
    quota, which is a different question with a different answer at
    Deadline 6, enforced under the user lock rather than at this boundary.

    The interesting failure is the 409 below, and it is the one invariant
    in the system that **Postgres enforces with no application lock
    deciding it**: `no_overlapping_room_reservations` is an
    `EXCLUDE USING gist` constraint, partial on `status = 'ACTIVE'`, so
    two concurrent bookings of the same slot cannot both commit no matter
    how the application is written. (The transaction does take a SHARE
    lock on the resource row, for the BLOCKED gate -- a different
    invariant. `reserve_room`'s docstring says why that one is shared
    rather than exclusive, and why it matters that it is.)

    The contrast with the GPU path at Deadline 4 -- where no constraint
    can express "this user holds at most 2" and a row lock is the only
    mechanism there is -- is the point the README makes with these two
    side by side.
    """
    try:
        reservation = service.reserve_room(db, room_id, user, payload)
    except service.RoomBlocked:
        # Outstanding item 6, ratified at Deadline 3. Distinct from both
        # existing 409s because the remedy is distinct: try a different
        # room. Do not wait for capacity, and do not release something you
        # already hold.
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "RESOURCE_BLOCKED",
            "This room is blocked and is not accepting new reservations.",
        )
    except service.RoomIntervalConflict:
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "INTERVAL_CONFLICT",
            "That interval overlaps an existing reservation for this room.",
        )

    if reservation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")

    return reservation


@router.delete(
    "/{room_id}/reservations/{reservation_id}", response_model=ReservationRead
)
def cancel_room_reservation(
    room_id: int,
    reservation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReservationRead:
    """Release a room hold. Owner or ADMIN.

    Mirrors the POST path, which is outstanding item 8 ratified at
    Deadline 4: `DELETE /reservations/{id}` was ambiguous because room
    holds live in `reservations` and GPU holds in `gpu_reservations`,
    each with its own id sequence, so one id named a row in both.

    Ownership is checked in the service rather than by `require_role`,
    which cannot express "owner OR admin" from a token alone.
    """
    try:
        reservation = service.cancel_reservation(db, room_id, reservation_id, user)
    except service.NotOwner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden")

    if reservation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reservation not found")

    return reservation
