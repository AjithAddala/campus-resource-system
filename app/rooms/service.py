from datetime import datetime

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import ReservationStatus, ResourceStatus, ResourceType
from app.models.reservation import Reservation
from app.models.resource import Resource, Room
from app.models.user import User
from app.rooms.schemas import ReservationCreate, RoomCreate


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


def create_room(db: Session, payload: RoomCreate) -> Room:
    """Create a room. Same joined-table note as `gpus.service.create_cluster`:
    constructing the subclass writes the `resources` row too and sets the
    discriminator from `polymorphic_identity`.
    """
    room = Room(
        name=payload.name, building=payload.building, capacity=payload.capacity
    )
    db.add(room)
    db.commit()
    db.refresh(room)
    return room


class RoomIntervalConflict(Exception):
    """The exclusion constraint rejected the insert: someone else holds an
    overlapping slot on this room.
    """


class RoomBlocked(Exception):
    """The room is BLOCKED. Outstanding item 6, ratified at Deadline 3."""


def _is_overlap_violation(exc: IntegrityError) -> bool:
    """True only for `no_overlapping_room_reservations`.

    Mapping every IntegrityError to "slot taken" would be a lie the first
    time a different constraint fires -- a bad `user_id` FK, say -- and it
    would report a foreign-key bug to the caller as a booking conflict.
    The constraint name is read from psycopg's diagnostics rather than by
    matching on the message text, which is localised and reworded between
    server versions.
    """
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", None) == "no_overlapping_room_reservations"


def reserve_room(
    db: Session, room_id: int, user: User, payload: ReservationCreate
) -> Reservation | None:
    """Hold a room for an interval.

    Returns the committed row, or **None if `room_id` is not a room** --
    the same None-means-404 convention `get_room` and `get_cluster`
    already use, so the router maps it the same way. Raises `RoomBlocked`
    or `RoomIntervalConflict` for the two domain refusals; this module
    knows no status codes.

    ------------------------------------------------------------------
    LOCK ORDER. The global rule (DECISIONS.md, "Lock ordering") is
    user row -> resource row, with NO exceptions anywhere in the codebase,
    because a single path taking them the other way round reintroduces the
    deadlock cycle for every path.

    This transaction takes only the SECOND of those locks, and that is
    consistent rather than an exception: room quota lands at Deadline 6,
    when A adds the user-row lock and the held-reservations count. The
    user lock belongs immediately BELOW this docstring and ABOVE the
    resource lock, and there is deliberately nothing between them now, so
    inserting it is an addition rather than a reordering.

    Taking the user lock today would be a lock protecting no invariant --
    the project's own standard is that complexity is earned.
    ------------------------------------------------------------------

    Why the resource row is locked at all, when rooms have no counter:
    the `status` gate below is mutable state on that row. Read without the
    lock, an admin could flip a room to BLOCKED between the read and the
    INSERT and the booking would land on a blocked room. Locking the row
    the gate reads is what closes that window -- and it is the same shape
    as the GPU path at Deadline 4, where the locked row also carries
    `allocated`.

    Note what is NOT done here: no "is this slot free?" SELECT. The same
    reasoning as `auth/service.py::register` -- such a check passes
    exactly when it does not matter, since two concurrent bookings can
    both run it and both see free. `no_overlapping_room_reservations` is
    what makes the second INSERT impossible, so the INSERT is the check.
    The overlap invariant is enforced entirely by Postgres, and no
    application lock participates in it.

    **FOR SHARE, not FOR UPDATE, and the distinction is the whole reason
    the sentence above is still true.** This transaction never writes the
    resource row -- rooms have no counter, unlike `GPUCluster.allocated`
    at Deadline 4, which is why that path takes the exclusive lock and
    this one does not. What the gate needs is for `status` not to CHANGE
    under it: an admin's PATCH must not land between the check and the
    INSERT. `FOR SHARE` says exactly that -- it blocks writers to this row
    until commit, and does not block other bookers.

    Taking `FOR UPDATE` here would also be correct, and it would quietly
    serialize every booking of one room behind every other, making the
    application lock -- not the constraint -- the thing deciding who wins
    a concurrent slot race. The invariant would hold and the design claim
    would not. (Serialized or not, both are correct; this is an argument
    about which mechanism is load-bearing, not a measured throughput
    claim. Deadline 5's harness is what could measure it.)
    """
    # Locks the `resources` row -- the base table, which is where `status`
    # lives. Querying the base class also avoids joining `rooms`, whose
    # columns this path never reads. `read=True` emits FOR SHARE.
    resource = (
        db.query(Resource)
        .filter(Resource.id == room_id)
        .with_for_update(read=True)
        .first()
    )

    # A 404, not a 403 or a 422. `reservations.resource_id` points at
    # `resources`, so nothing at the DATABASE level stops a "room" booking
    # naming a GPU cluster -- the discriminator check that read paths get
    # free from polymorphic loading has to be made by hand on write paths.
    # Both "no such id" and "that id is a GPU cluster" answer the same way:
    # there is no such room.
    if resource is None or resource.resource_type is not ResourceType.ROOM:
        return None

    # Outstanding item 6, ratified at Deadline 3: BLOCKED stops NEW
    # allocations and does not evict existing ones -- the same rule as the
    # capacity reduction in ARCHITECTURE_AND_WORKFLOWS.md section 13. The
    # remedy differs from both 409s that already exist (try a different
    # resource; do not wait, do not release anything you hold), so it
    # carries its own code.
    #
    # The database enforces none of this: the GiST constraint is partial on
    # the RESERVATION's status, not the resource's. This gate is the whole
    # guarantee, which is why it reads the row it just locked.
    if resource.status is ResourceStatus.BLOCKED:
        raise RoomBlocked(room_id)

    reservation = Reservation(
        resource_id=room_id,
        user_id=user.id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        status=ReservationStatus.ACTIVE,
    )
    db.add(reservation)

    try:
        db.commit()
    except IntegrityError as exc:
        # Rollback FIRST: after an IntegrityError the session is in a
        # failed state and any further statement raises
        # PendingRollbackError, which would surface as a 500 and bury the
        # 409 it was meant to produce. Same trap as duplicate registration.
        db.rollback()
        if _is_overlap_violation(exc):
            raise RoomIntervalConflict(room_id) from exc
        raise

    db.refresh(reservation)
    return reservation
