from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import ReservationStatus, ResourceStatus, ResourceType, Role
from app.models.reservation import Reservation
from app.models.resource import Resource, Room
from app.models.user import User
from app.quotas import service as quotas
from app.rooms.schemas import ReservationCreate, RoomCreate, RoomUpdate


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


class NotOwner(Exception):
    """Caller is neither the owner of this reservation nor an ADMIN."""


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

    **Deadline 6 inserted the first of those locks, exactly where the
    Deadline 3 comment said it would go** -- above the resource lock, with
    nothing moved. The room quota is now the invariant that earns it:
    "this user holds at most N concurrent room reservations" is a fact
    about the USER, and no resource lock can see it, because two bookings
    of two DIFFERENT rooms contend on no common row. That is the same
    failure shape as the cross-cluster GPU race, on a third invariant.

      (1) LOCK the user row   -> guards the room quota
      (2) LOCK the room row   -> guards the BLOCKED gate (FOR SHARE)
      (3) INSERT              -> the exclusion constraint guards overlap
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
    # --- (1) QUOTA GATE, Deadline 6 -------------------------------------
    # Taken FIRST, per the global order, and held to COMMIT. Note this
    # happens before the room is known to exist -- the existence check is
    # the `resource is None` branch below, which now runs while holding a
    # lock on the caller's own row. That costs one user-row lock for a
    # request that 404s, and it buys the ordering: acquiring the user lock
    # after the resource lock in even one path is what reintroduces the
    # deadlock cycle for every path.
    db.execute(select(User.id).where(User.id == user.id).with_for_update())

    # Read AFTER the lock, always -- the same rule as the GPU gate. A
    # count read before the lock is a count that can move before the
    # INSERT, which is precisely the race being closed.
    quotas.enforce_room_quota(db, user.id, user.role)

    # --- (2) BLOCKED GATE -----------------------------------------------
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


def cancel_reservation(
    db: Session, room_id: int, reservation_id: int, user: User
) -> Reservation | None:
    """Release a room hold. Owner or ADMIN. Returns None if not found.

    **Deadline 6 gave this transaction a lock it did not need before**,
    exactly as the Deadline 3 docstring predicted. It still touches no
    shared counter — unlike `gpus.service.cancel_gpu_reservation`, which
    decrements `GPUCluster.allocated`. What changed is that
    `held_room_reservations` now COUNTs ACTIVE rows for the owner, so
    flipping this row's status *is* a write to a quantity the room quota
    reads. Without the user lock, a cancel and a concurrent booking by the
    same user interleave: the booking counts this row as still held,
    refuses at the limit, and the caller is told they are at quota by a
    hold that no longer exists.

    The lock is on the reservation's **OWNER**, not the caller — an ADMIN
    releasing someone else's hold must serialize against that user's
    bookings, and locking the admin's own row would protect nothing. Same
    rule, same reason, as the GPU cancel path.

    Two concurrent cancels of the same row: the second finds
    `status = CANCELLED` and returns. There is still nothing to
    double-decrement, so that check answers consistently rather than
    protecting a number.
    """
    reservation = db.get(Reservation, reservation_id)

    # Item 8, ratified at Deadline 4: the route mirrors the POST, so the
    # room id in the path must actually name this reservation's room.
    # Without this check the path segment is decorative and
    # /rooms/4/reservations/5 would cancel a hold on room 3.
    if reservation is None or reservation.resource_id != room_id:
        return None

    if reservation.user_id != user.id and user.role is not Role.ADMIN:
        raise NotOwner(reservation_id)

    # Cheap pre-check outside the lock: an already-cancelled hold needs no
    # lock at all. Re-checked under the lock below, because this read can
    # go stale -- this one only avoids taking a lock needlessly.
    if reservation.status is ReservationStatus.CANCELLED:
        return reservation

    # Deadline 6: lock the OWNER's row, so this release serializes against
    # that user's bookings. See the docstring.
    db.execute(
        select(User.id).where(User.id == reservation.user_id).with_for_update()
    )

    # Re-read under the lock. A concurrent cancel of this same row could
    # have committed between the pre-check and here.
    db.refresh(reservation)
    if reservation.status is ReservationStatus.CANCELLED:
        return reservation

    reservation.status = ReservationStatus.CANCELLED
    db.commit()
    db.refresh(reservation)
    return reservation


# ---------------------------------------------------------------------------
# Admin resource-status writes — Deadline 6
# ---------------------------------------------------------------------------


def update_room(db: Session, room_id: int, payload: RoomUpdate) -> Room | None:
    """Admin edit of a room. Returns None if `room_id` is not a room.

    **This endpoint WRITES `resources.status`; Deadlines 3 and 4 built the
    gates that READ it.** That split is the whole point of outstanding
    item 6: blocking is admin-controlled mutable state, which is why both
    gates read it under the row lock rather than at their boundary, and
    why this handler can flip it without knowing anything about
    reservations.

    Blocking does **not** evict existing holds — the rule ratified at
    Deadline 3 and asserted in `check_rooms.py`. An active reservation in
    a room that is later blocked stays valid; only new bookings are
    refused. So there is nothing to cascade here and no lock to take: the
    booking path reads this column under `FOR SHARE`, so a concurrent
    booking either sees AVAILABLE and commits before this UPDATE can
    proceed, or waits and then sees BLOCKED. The serialization is already
    paid for by the reader.
    """
    room = get_room(db, room_id)
    if room is None:
        return None

    # `exclude_unset` so that omitting a field means "leave it alone"
    # rather than "set it to null". PATCH is a partial update, and a
    # Pydantic model with `None` defaults cannot tell the two apart
    # without this.
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(room, field, value)

    db.commit()
    db.refresh(room)
    return room
