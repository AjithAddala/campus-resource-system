from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.enums import ReservationStatus
from app.models.reservation import Reservation
from app.models.resource import Room


def list_rooms(db: Session) -> list[Room]:
    return db.query(Room).order_by(Room.id).all()


def get_room(db: Session, room_id: int) -> Room | None:
    """None for an id that exists but is not a room — see gpus/service.py."""
    return db.query(Room).filter(Room.id == room_id).first()


def conflicting_reservations(
    db: Session, room_id: int, start: datetime, end: datetime
) -> list[Reservation]:
    """Active reservations for this room overlapping [start, end).

    The predicate is written to match `no_overlapping_room_reservations`
    exactly, because a mismatch here is worse than no endpoint at all:
    it would report a slot free that the constraint then rejects, and the
    disagreement would look like a concurrency bug.

    Two details carry that match:

    - `'[)'` — half-open, inclusive start and exclusive end. With `'[]'`
      a booking ending at 12:00 would conflict with one starting at
      12:00, and adjacent bookings are supposed to succeed.
    - `status = ACTIVE` — the constraint is partial on the same
      condition, so a cancelled reservation must not block its old slot.

    `&&` is the range-overlap operator, the same one the exclusion
    constraint uses, so this read is answered by the GiST index that
    constraint created rather than by a second index of our own.
    """
    window = func.tstzrange(start, end, "[)")
    booked = func.tstzrange(Reservation.start_time, Reservation.end_time, "[)")

    return (
        db.query(Reservation)
        .filter(
            Reservation.resource_id == room_id,
            Reservation.status == ReservationStatus.ACTIVE,
            booked.op("&&")(window),
        )
        .order_by(Reservation.start_time)
        .all()
    )
