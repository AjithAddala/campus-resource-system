import logging

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.course import Course, CourseOffering
from app.models.enrollment import Enrollment, WaitlistEntry
from app.models.enums import EnrollmentStatus
from app.models.user import User
from app.quotas import service as quotas

# Promotion passes candidates over silently -- no row changes when a
# student is skipped -- so the skip is logged. Without it a student can
# lose their turn in the queue with no record anywhere that it happened.
log = logging.getLogger(__name__)


def list_courses(db: Session) -> list[Course]:
    return db.query(Course).order_by(Course.code).all()


def get_course(db: Session, course_id: int) -> Course | None:
    return db.get(Course, course_id)


def list_offerings(db: Session, course_id: int) -> list[CourseOffering]:
    return (
        db.query(CourseOffering)
        .filter(CourseOffering.course_id == course_id)
        .order_by(CourseOffering.year, CourseOffering.semester, CourseOffering.id)
        .all()
    )


def get_offering(db: Session, offering_id: int) -> CourseOffering | None:
    return db.get(CourseOffering, offering_id)


# ---------------------------------------------------------------------------
# Registration — Deadline 4
# ---------------------------------------------------------------------------


class OfferingFull(Exception):
    """No seats left. The offering-row analogue of CapacityExhausted."""

    def __init__(self, capacity: int, enrolled: int):
        self.capacity = capacity
        self.enrolled = enrolled
        super().__init__(f"{enrolled}/{capacity} seats taken")


class AlreadyEnrolled(Exception):
    """The student already holds an ACTIVE enrollment in this offering."""


class ScheduleConflict(Exception):
    """This offering meets at the same time as one the student already has."""

    def __init__(self, other: CourseOffering):
        self.other = other
        super().__init__(
            f"conflicts with offering {other.id} ({other.days} "
            f"{other.start_time}-{other.end_time})"
        )


class NotEnrolled(Exception):
    """Drop was called by a student with no enrollment row at all."""


# Single-character day codes. R is Thursday, U is Sunday.
#
# **Multi-character tokens like "Tu"/"Th" are deliberately not supported**,
# and the reason is a bug rather than laziness: overlap is computed by
# intersecting the sets of characters, and `set("Tu") & set("Th")` is
# `{"T"}` -- so a Tuesday class would be reported as conflicting with a
# Thursday one. One character per day makes the intersection exact. The
# seed uses "MWF"; if an offering-creation endpoint ever lands, this is
# the vocabulary it must validate against.
DAY_CODES = frozenset("MTWRFSU")


def _days_overlap(a: str, b: str) -> bool:
    return bool(set(a) & set(b))


def _times_overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    """Half-open comparison on zero-padded "HH:MM" strings.

    Lexicographic ordering is correct here **only because the values are
    zero-padded** -- "9:00" would sort after "10:30" and silently invert
    every comparison. That is why DECISIONS.md insists on "09:00", and why
    the seed writes it that way.

    Half-open (`<`, not `<=`) for the same reason the room constraint uses
    `'[)'`: a class ending at 10:30 does not conflict with one starting at
    10:30.
    """
    return a_start < b_end and b_start < a_end


def _conflicting_offering(
    db: Session, student_id: int, target: CourseOffering
) -> CourseOffering | None:
    """An active enrollment of this student that clashes with `target`.

    **The caller must already hold the student's user-row lock.** This
    reads rows that a concurrent registration could be inserting: a
    schedule clash is a fact about the STUDENT, exactly like a quota, so
    it is guarded by the same lock and not by the offering lock. Two
    simultaneous registrations for two different-but-overlapping offerings
    contend on no offering row at all.
    """
    rows = db.execute(
        select(CourseOffering)
        .join(Enrollment, Enrollment.course_offering_id == CourseOffering.id)
        .where(
            Enrollment.student_id == student_id,
            Enrollment.status == EnrollmentStatus.ACTIVE,
            CourseOffering.id != target.id,
        )
    ).scalars()

    for other in rows:
        if _days_overlap(other.days, target.days) and _times_overlap(
            other.start_time, other.end_time, target.start_time, target.end_time
        ):
            return other
    return None


def register(db: Session, offering_id: int, student: User) -> Enrollment | None:
    """Take a seat in an offering. Returns None if the offering does not exist.

    Raises `AlreadyEnrolled`, `ScheduleConflict` or `OfferingFull`.

    ==================================================================
    LOCK ORDER -- USER ROW FIRST, OFFERING ROW SECOND.
    ==================================================================
    Identical to the GPU transaction, and it must be: the offering row
    plays the part the cluster row plays there. It is the row holding the
    counter, so it is the row that gets locked.

      (1) LOCK the student's user row  -> guards the SCHEDULE check, which
                                          is a fact about the student
      (2) LOCK the offering row        -> guards the SEAT count, which is
                                          a fact about the offering
      (3) upsert the enrollment and bump enrolled_count, together
      (4) COMMIT

    **Why the user lock is here at Deadline 4 rather than waiting for the
    course-load quota at Deadline 6.** The plan sketches this transaction
    as offering-lock-only, and that would be correct for capacity alone.
    But the schedule-overlap check reads the student's OTHER enrollments,
    and two concurrent registrations for two different offerings that
    clash with each other touch no common offering row -- so nothing would
    serialize them and both would pass. That is the same failure shape as
    the cross-cluster GPU quota race, on a different invariant. The lock
    is earned here; it is not being taken early for tidiness.

    Deadline 6 therefore adds the course-load quota INSIDE a lock that is
    already held, which is an addition rather than a reordering.
    """
    offering = get_offering(db, offering_id)
    if offering is None:
        return None

    # (1) user row.
    db.execute(select(User.id).where(User.id == student.id).with_for_update())

    existing = db.execute(
        select(Enrollment).where(
            Enrollment.student_id == student.id,
            Enrollment.course_offering_id == offering_id,
        )
    ).scalar_one_or_none()

    # `enrollment_unique` is UNCONDITIONAL -- it has no `WHERE status =
    # 'ACTIVE'` clause -- so a student who dropped STILL OWNS A ROW. That
    # makes re-registration an UPDATE, not an INSERT, and getting this
    # wrong would surface as an IntegrityError from inside the transaction
    # rather than as the 409 it should be. The same trap catches waitlist
    # promotion at Deadline 7.
    if existing is not None and existing.status is EnrollmentStatus.ACTIVE:
        raise AlreadyEnrolled(offering_id)

    # --- COURSE-LOAD QUOTA, Deadline 6 ----------------------------------
    # Inside the user lock taken above -- an addition, not a reordering,
    # exactly as the Deadline 4 write-up predicted. Nothing moved.
    #
    # **Gate order: after ALREADY_ENROLLED, before SCHEDULE_CONFLICT and
    # OFFERING_FULL**, and each boundary is deliberate:
    #
    #   after AlreadyEnrolled  a caller who already holds this seat should
    #                          be told that, not told they are at their
    #                          limit -- they are asking about a seat they
    #                          have, and the count includes it.
    #   before ScheduleConflict / OfferingFull
    #                          a student at their limit cannot register
    #                          for ANY offering, so a clash or a full
    #                          class is a detail about a request that was
    #                          never going to succeed. This also matches
    #                          the GPU path, where `check_gpus.py` asserts
    #                          quota fires before capacity.
    #
    # A DROPPED row does not count, so re-registering after a drop is
    # correctly seen as acquiring a seat the student does not hold.
    quotas.enforce_course_quota(db, student.id, student.role)

    conflict = _conflicting_offering(db, student.id, offering)
    if conflict is not None:
        raise ScheduleConflict(conflict)

    # (2) offering row. Re-read under the lock: `enrolled_count` from the
    # unlocked read above would be exactly the stale value that lets 500
    # concurrent registrations take 50 seats twice over.
    #
    # `populate_existing()` is LOAD-BEARING and its absence is invisible.
    # `get_offering` above put this row in the Session's identity map,
    # and by default a SELECT that returns an already-identity-mapped
    # row hands back the EXISTING object WITHOUT refreshing its
    # attributes. So `FOR UPDATE` takes the lock -- the SQL is right,
    # the lock is real -- and then the gate below reads the value from
    # BEFORE the lock. Proven directly: session A reads 0, session B
    # commits 41, A's `SELECT ... FOR UPDATE` still says 0, and with
    # populate_existing says 41.
    #
    # Measured cost of omitting it: 20 concurrent registrations for 5
    # seats all returned 201, with enrolled_count landing on 3. Not an
    # off-by-one -- lost updates, because every transaction
    # incremented the same stale number.
    seat_read = (
        select(CourseOffering)
        .where(CourseOffering.id == offering_id)
        .execution_options(populate_existing=True)
    )
    # Benchmark 1's broken build removes ONLY the lock. The read stays
    # fresh (`populate_existing` above), so what the benchmark measures is
    # not a stale value -- it is a correct value that nothing was holding
    # still between the check and the increment. Default is locked.
    if not get_settings().BENCHMARK_UNSAFE_NO_OFFERING_LOCK:
        seat_read = seat_read.with_for_update()
    locked = db.execute(seat_read).scalar_one()

    if locked.enrolled_count >= locked.capacity:
        # 409 today. Deadline 7 decides whether a full offering instead
        # falls through to the waitlist (outstanding item 10) -- that is a
        # policy question about the API, not about this lock.
        raise OfferingFull(locked.capacity, locked.enrolled_count)

    # (3) The enrollment row and the counter are written in the SAME
    # transaction, always. `enrolled_count` is derived state: any path
    # that updates one without the other makes the two disagree, which is
    # what Deadline 8's reconciliation query exists to detect.
    if existing is None:
        enrollment = Enrollment(
            student_id=student.id,
            course_offering_id=offering_id,
            status=EnrollmentStatus.ACTIVE,
        )
        db.add(enrollment)
    else:
        existing.status = EnrollmentStatus.ACTIVE
        enrollment = existing

    locked.enrolled_count += 1

    # --- clear any queue place for THIS offering ------------------------
    # **A seat and a place in the queue are mutually exclusive states**, an
    # invariant `join_waitlist` states and enforces on its own side but
    # which this path could violate: registering directly for a seat that
    # fell free left the student's waitlist entry behind.
    #
    # It was reachable and it lost a seat. Found reviewing the promotion
    # transaction at Deadline 7, reproduced end to end:
    #
    #   X queues for a full offering; a drop frees the seat but promotion
    #   SKIPS X (schedule clash); X clears the clash and registers
    #   directly; X now holds a seat AND a queue place; the next drop
    #   promotes X again -- `enrolled_count` 2 against 1 ACTIVE row.
    #
    # The seat was gone: the counter said taken and no student held it,
    # and Deadline 8's reconciliation query is what would eventually have
    # reported it, long after the cause.
    #
    # Deleted here rather than guarded in promotion alone, because this is
    # the path that CREATES the inconsistent state -- `_promote_one` also
    # skips an already-enrolled candidate now, but that is a backstop, not
    # the fix. Safe under the locks this transaction already holds: the
    # user row from step (1) blocks a concurrent join by the same student,
    # and joins take the offering FOR SHARE, which this FOR UPDATE
    # excludes.
    db.execute(
        delete(WaitlistEntry).where(
            WaitlistEntry.student_id == student.id,
            WaitlistEntry.course_offering_id == offering_id,
        )
    )

    db.commit()
    db.refresh(enrollment)
    return enrollment


def drop(db: Session, offering_id: int, student: User) -> Enrollment | None:
    """Release a seat. Returns None if the offering does not exist.

    Raises `NotEnrolled` when the student never had a row here.

    **Naturally idempotent**, like GPU cancellation: dropping an already
    DROPPED enrollment returns the same row and does not decrement the
    counter twice. The status flag is what makes that safe -- the
    decrement happens only on the ACTIVE -> DROPPED transition.

    Same lock order as registration, for the same reason: Deadline 7 hangs
    waitlist promotion off this transaction, and promotion touches another
    student's row. Getting the order right here is what stops that from
    becoming a deadlock later.
    """
    offering = get_offering(db, offering_id)
    if offering is None:
        return None

    db.execute(select(User.id).where(User.id == student.id).with_for_update())

    # Benchmark 4's broken column removes ONLY this lock, exactly as
    # Benchmark 1 does for `register`. Everything else -- including
    # `populate_existing()` -- stays, so the broken build reads a FRESH
    # waitlist and still promotes the same entry twice: two droppers each
    # read the same oldest candidate because nothing serialized them on
    # the offering row. Default is locked.
    seat_read = (
        select(CourseOffering)
        .where(CourseOffering.id == offering_id)
        .execution_options(populate_existing=True)
    )
    if not get_settings().BENCHMARK_UNSAFE_NO_OFFERING_LOCK:
        seat_read = seat_read.with_for_update()
    locked = db.execute(seat_read).scalar_one()

    enrollment = db.execute(
        select(Enrollment).where(
            Enrollment.student_id == student.id,
            Enrollment.course_offering_id == offering_id,
        )
    ).scalar_one_or_none()

    if enrollment is None:
        raise NotEnrolled(offering_id)

    if enrollment.status is EnrollmentStatus.DROPPED:
        return enrollment

    enrollment.status = EnrollmentStatus.DROPPED
    locked.enrolled_count -= 1

    # --- PROMOTION, Deadline 7 ------------------------------------------
    # In THIS transaction, holding both locks the drop already took. The
    # seat is not released and then re-taken: it moves from one student to
    # another atomically, so there is never a moment when a queued student
    # could lose it to an ordinary registration.
    _promote_one(db, locked)

    db.commit()
    db.refresh(enrollment)
    return enrollment


# ---------------------------------------------------------------------------
# Waitlist — Deadline 7, B's column (the endpoints; promotion is A's)
# ---------------------------------------------------------------------------
#
# Joining is EXPLICIT: `POST /offerings/{id}/waitlist`, never a
# fall-through from a full `register`. Outstanding item 10, A's proposal
# and B's response both agreeing, because auto-waitlisting would make one
# `201` from `register` mean either "you have a seat" or "you are queued"
# -- the same defect Deadline 5 refused when it settled the replay status.
#
# Item 7 follows and is enforced by construction: no code path below
# writes `EnrollmentStatus.WAITLISTED`. A queued student has a row in
# `waitlist_entries` and nothing else. Putting the same fact in two tables
# is the failure this project has now found four times.


class OfferingNotFull(Exception):
    """Tried to queue for an offering that still has seats.

    Refused rather than silently accepted: a queue for an available seat
    is not a queue, and a student holding a waitlist entry on a section
    they could simply register for would be waiting for a promotion that
    only fires on a DROP. The remedy is to register.
    """

    def __init__(self, capacity: int, enrolled: int):
        self.capacity = capacity
        self.enrolled = enrolled
        super().__init__(f"{enrolled}/{capacity} seats taken, not full")


class AlreadyWaitlisted(Exception):
    """The student already holds a waitlist entry for this offering."""


class NotWaitlisted(Exception):
    """Leave was called by a student with no waitlist entry here."""


def _positions(db: Session, offering_id: int) -> list[tuple[WaitlistEntry, int]]:
    """Every entry for one offering, oldest first, paired with its position.

    **Position is computed at read time and never stored.** There is no
    `position` column -- it was dropped in revision `c86676652ca2` -- and
    its absence is load-bearing rather than a simplification: renumbering
    a stored position after a promotion transiently violates a unique
    constraint mid-UPDATE, and every row after the promoted one would have
    to be rewritten. `ROW_NUMBER()` makes a promotion touch exactly one
    row: the one it deletes.

    **`ORDER BY created_at, id`, and the `id` tiebreak is the whole
    guarantee, not a formality.** `func.now()` is TRANSACTION start time,
    so entries written inside one transaction share a `created_at` to the
    microsecond and `created_at` alone cannot express FIFO between them.
    `scripts/check_waitlist.py` Part 1 proves that against the live
    database rather than asserting it in prose.

    One definition of position, used by both the GET endpoint and the
    number reported on a successful join -- so the two can never drift.
    """
    position = (
        func.row_number()
        .over(order_by=(WaitlistEntry.created_at, WaitlistEntry.id))
        .label("position")
    )
    rows = db.execute(
        select(WaitlistEntry, position)
        .where(WaitlistEntry.course_offering_id == offering_id)
        .order_by(WaitlistEntry.created_at, WaitlistEntry.id)
    ).all()
    return [(entry, pos) for entry, pos in rows]


def list_waitlist(
    db: Session, offering_id: int
) -> list[tuple[WaitlistEntry, int]] | None:
    """The queue for an offering. None if the offering does not exist.

    **Read-only, and it takes no lock** -- the same rule `GET /me/quota`
    follows. A position shown to a caller is a display value that may be
    stale by the time they read it; the promotion transaction is what has
    to be right, and it recomputes the order under the offering lock.
    Taking a lock here would serialize browsing against promoting and buy
    a number that is stale anyway the moment it is serialized.
    """
    if get_offering(db, offering_id) is None:
        return None
    return _positions(db, offering_id)


def join_waitlist(
    db: Session, offering_id: int, student: User
) -> tuple[WaitlistEntry, int] | None:
    """Queue for a seat in a FULL offering. None if the offering does not exist.

    Raises `AlreadyEnrolled`, `AlreadyWaitlisted` or `OfferingNotFull`.

    ==================================================================
    LOCK ORDER -- USER ROW FIRST, OFFERING ROW SECOND. AS EVERYWHERE.
    ==================================================================
    Same global order as allocation, registration and cancellation, and
    it matters more here than anywhere else in B's code: Deadline 7 is
    the deadline where a second path starts touching two user rows, and
    outstanding item 9 exists because promotion cannot obey this order.
    Every path that CAN obey it must, or the argument that promotion is
    the sole exception stops being true.

      (1) LOCK the student's user row   FOR UPDATE
      (2) LOCK the offering row         FOR SHARE
      (3) INSERT the entry, COMMIT

    **Why the user row is locked at all.** Without it, a student's
    concurrent `register` and `join_waitlist` touch no common row: the
    register writes an enrollment, the join writes a waitlist entry, and
    the student ends up holding a seat AND queueing for it. That is the
    two-tables-one-fact failure again, arriving through a race rather
    than through a schema choice. The user row is the only thing both
    paths share, exactly as it is for the cross-cluster GPU quota.

    **Why the offering row is FOR SHARE and not FOR UPDATE.** This
    transaction READS the offering to decide fullness and never writes
    it -- the same distinction that made the room gate a share lock at
    Deadline 3, while the GPU and registration gates take FOR UPDATE
    because they write `allocated` / `enrolled_count`. A share lock still
    excludes the writers, which is the whole requirement: `register` and
    `drop` both take FOR UPDATE, so a seat cannot appear or vanish
    between the fullness check below and this transaction's commit.

    **What FOR SHARE buys against promotion, specifically.** Promotion
    runs inside `drop`, holding the offering row FOR UPDATE while it
    scans candidates. A join therefore cannot commit an entry in the
    middle of that scan -- it waits for the drop to finish, and then sees
    a settled queue. And the reverse direction cannot deadlock: promotion
    takes candidate user rows `SKIP LOCKED` (item 9), so a promotion that
    meets this transaction's user lock skips that candidate rather than
    waiting for a transaction that is itself waiting on the offering.
    Neither side waits on the other; that is item 9's proposal doing the
    job it was proposed for.

    **No course-load quota check.** Queueing costs nothing and holds
    nothing: `held_course_enrollments` counts ACTIVE enrollments and a
    queued student has no enrollment row at all. A student at their cap
    of 6 may still queue, and the quota is enforced at PROMOTION time,
    where A's transaction checks it and skips a candidate who would
    breach. Charging quota here would refuse a student something they
    are not yet receiving. Recorded with item 10's ratification.
    """
    offering = get_offering(db, offering_id)
    if offering is None:
        return None

    # (1) user row.
    db.execute(select(User.id).where(User.id == student.id).with_for_update())

    # A seat and a place in the queue are mutually exclusive states, and
    # this is the check that keeps them so. Held under the user lock, so a
    # concurrent register cannot slip a seat in behind it.
    enrollment = db.execute(
        select(Enrollment).where(
            Enrollment.student_id == student.id,
            Enrollment.course_offering_id == offering_id,
        )
    ).scalar_one_or_none()
    if enrollment is not None and enrollment.status is EnrollmentStatus.ACTIVE:
        raise AlreadyEnrolled(offering_id)

    # (2) offering row, FOR SHARE. `populate_existing` for the reason
    # `register` documents at length: `get_offering` above put this row in
    # the Session's identity map, and a SELECT returning an
    # already-mapped row hands back the existing object WITHOUT
    # refreshing it -- so the lock would be real and the value read under
    # it would be from before the lock. That bug is invisible in review
    # and cost 20-of-5 seats in the course path at Deadline 4.
    locked = db.execute(
        select(CourseOffering)
        .where(CourseOffering.id == offering_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).scalar_one()

    if locked.enrolled_count < locked.capacity:
        raise OfferingNotFull(locked.capacity, locked.enrolled_count)

    # Pre-checked under the user lock rather than caught from
    # `waitlist_unique`, and here that is genuinely sufficient rather than
    # merely convenient: two concurrent joins by the SAME student
    # serialize on the user row taken in step (1), so the second one reads
    # the first one's committed entry. The UNIQUE constraint stays as the
    # backstop it is everywhere else -- it is what would make a mistake
    # here fail loudly instead of queueing one student twice.
    existing = db.execute(
        select(WaitlistEntry).where(
            WaitlistEntry.student_id == student.id,
            WaitlistEntry.course_offering_id == offering_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise AlreadyWaitlisted(offering_id)

    entry = WaitlistEntry(student_id=student.id, course_offering_id=offering_id)
    db.add(entry)
    db.commit()
    db.refresh(entry)

    # Reported from the same `_positions` query the GET endpoint uses, so
    # "you are 3rd" means exactly what `GET /waitlist` will say. Read
    # after the commit and without a lock: it is a display value.
    position = next(p for e, p in _positions(db, offering_id) if e.id == entry.id)
    return entry, position


def leave_waitlist(
    db: Session, offering_id: int, student: User
) -> WaitlistEntry | None:
    """Give up a place in the queue. None if the offering does not exist.

    Raises `NotWaitlisted`.

    **Deletes the row rather than flagging it.** Unlike an enrollment,
    which keeps a DROPPED row because `enrollment_unique` is
    unconditional and re-registration must be an UPDATE, a waitlist entry
    carries no history anyone reads and its absence IS the state. A
    `LEFT` status would also have to be excluded from every FIFO read,
    and forgetting that exclusion in the promotion query would promote a
    student who had left.

    **Takes the offering row FOR SHARE, and that is not decoration.**
    Promotion (A's, item 9) runs inside `drop` holding the offering row
    FOR UPDATE: it reads the oldest entry, then attempts that student's
    user row. Without a lock here, this transaction could DELETE the very
    entry promotion has already selected, and promotion would hand a seat
    to a student who had left the queue -- writing an enrollment for
    somebody who asked to be removed. The share lock makes the leave wait
    for the promotion to commit, after which the row is either already
    gone (promoted) or still ours to delete.

    Lock order is the global one, user row first, for the same reason as
    `join_waitlist`.
    """
    offering = get_offering(db, offering_id)
    if offering is None:
        return None

    db.execute(select(User.id).where(User.id == student.id).with_for_update())

    db.execute(
        select(CourseOffering)
        .where(CourseOffering.id == offering_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).scalar_one()

    entry = db.execute(
        select(WaitlistEntry).where(
            WaitlistEntry.student_id == student.id,
            WaitlistEntry.course_offering_id == offering_id,
        )
    ).scalar_one_or_none()

    if entry is None:
        raise NotWaitlisted(offering_id)

    # Detached copy for the response: the row is about to stop existing,
    # and the caller is owed the entry they just gave up rather than a
    # 204 with nothing in it. Same shape as `drop` returning the DROPPED
    # enrollment.
    left = WaitlistEntry(
        id=entry.id,
        student_id=entry.student_id,
        course_offering_id=entry.course_offering_id,
        created_at=entry.created_at,
    )

    db.delete(entry)
    db.commit()

    # **Nothing is renumbered.** Everyone behind the departing student
    # moves up by one the next time a position is COMPUTED, because
    # position was never stored. This is the same property that makes
    # promotion a single-row delete.
    return left


# ---------------------------------------------------------------------------
# Waitlist promotion — Deadline 7
# ---------------------------------------------------------------------------
#
# **OWNERSHIP NOTE.** `EXECUTION_PLAN.md` assigns this transaction to A
# and the waitlist endpoints to B. It was written by B in session 17
# because A's column had not started and Deadline 7 could not close.
# It implements A's proposal (outstanding item 9) as written, with one
# addition flagged below. **A has not reviewed it.**


def _promote_one(db: Session, offering: CourseOffering) -> Enrollment | None:
    """Give the freed seat to the oldest ELIGIBLE queued student, or nobody.

    Called from inside `drop`, which already holds:

        user(dropper)   FOR UPDATE      <- the global lock order
        offering        FOR UPDATE      <- the row whose counter moves

    ==================================================================
    THE DEADLOCK THIS AVOIDS, AND WHY `SKIP LOCKED` RATHER THAN ORDER
    ==================================================================
    Outstanding item 9. Promotion needs a SECOND user row -- the
    candidate's -- to check their course-load quota under a lock. It
    cannot know which one until it has read the waitlist, and reading the
    waitlist consistently needs the offering lock it is already holding.
    So its order is offering -> user, while `register`'s is user ->
    offering:

        T1  X drops offering O      holds user(X) -> O, wants user(Y)
        T2  Y registers for O       holds user(Y),      wants O
                                    -> cycle, deadlock

    There is no ordering fix, because Y's identity is the *output* of the
    read that requires the lock. **So the wait is removed instead of
    ordered.** Each candidate's row is attempted `FOR UPDATE SKIP
    LOCKED`; a row that is not immediately free is skipped and the next
    candidate tried. A transaction that never blocks on a user row cannot
    appear in a cycle at all -- which is a stronger statement than
    promotion obeying the global order, and it means §14's "every path"
    claim needs no exception written into it.

    **The cost, stated rather than hidden: the promise is *oldest
    ELIGIBLE*, not *oldest*.** A queued student who happens to be doing
    something else at that instant is passed over, and nothing about
    their row changes when it happens. This is not a new concession --
    the Deadline 7 spec already defines FIFO over eligible entries by
    letting a quota-breaching candidate be skipped. This adds one clause
    to eligibility: "and their row is not currently locked".

    **The quota check stays under a real lock**, which is the half of
    item 9 that matters most. An unprotected quota check is precisely the
    failure Benchmark 2 exists to demonstrate, and shipping one in the
    promotion path would contradict the project's central claim.

    ==================================================================
    ONE ADDITION BEYOND A'S PROPOSAL -- NEEDS A'S REVIEW
    ==================================================================
    A's proposal skips a candidate whose **course-load quota** would
    breach. It says nothing about **schedule conflicts**, and without
    that check promotion can seat a student in a class that clashes with
    one they already hold -- a state `register` refuses outright, reached
    through a different door.

    A schedule clash is a fact about the student, guarded by the user
    lock, exactly like the quota. Both are checked here, and a candidate
    failing either is skipped rather than refused, because "refuse" has
    no meaning in a path nobody is waiting on. Flagged rather than
    folded in silently: it widens the eligibility rule that item 9
    ratified, so A should agree with it explicitly.
    """
    # Defensive, and cheap: promotion is only ever called from a path that
    # has just freed a seat, but the check makes this function safe to
    # call from anywhere later. Reading the counter off the row the caller
    # holds FOR UPDATE, never a boundary read.
    if offering.enrolled_count >= offering.capacity:
        return None

    # ORDER BY created_at, id -- and the `id` tiebreak is the entire FIFO
    # guarantee, not a formality. `func.now()` is TRANSACTION start time,
    # so entries written inside one transaction share a `created_at` to
    # the microsecond. `scripts/check_waitlist.py` Part 1 proves it on the
    # live database.
    #
    # Read under the offering lock, so the candidate list cannot change
    # underneath this loop: joins take the offering FOR SHARE and leaves
    # take it FOR SHARE, both of which conflict with the FOR UPDATE the
    # caller holds.
    candidates = (
        db.execute(
            select(WaitlistEntry)
            .where(WaitlistEntry.course_offering_id == offering.id)
            .order_by(WaitlistEntry.created_at, WaitlistEntry.id)
        )
        .scalars()
        .all()
    )

    for entry in candidates:
        # `SKIP LOCKED` returns no row rather than waiting. This is the
        # single line outstanding item 9 exists to justify.
        acquired = db.execute(
            select(User.id)
            .where(User.id == entry.student_id)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()

        if acquired is None:
            # The skip leaves a trace. Nothing about the entry changes
            # when a student is passed over, so without this line a
            # student can lose their turn with no record anywhere that it
            # happened. B's condition on ratifying item 9.
            log.info(
                "waitlist: skipped entry %s (student %s) on offering %s -- "
                "user row busy",
                entry.id,
                entry.student_id,
                offering.id,
            )
            continue

        # Fresh read under the lock just taken. `populate_existing` for
        # the reason `register` documents at length -- an
        # already-identity-mapped row otherwise comes back with its
        # pre-lock attribute values, which is invisible in review and
        # cost 20-of-5 seats at Deadline 4.
        candidate = db.execute(
            select(User)
            .where(User.id == entry.student_id)
            .execution_options(populate_existing=True)
        ).scalar_one()

        # --- already holds a seat here? -------------------------------
        # A queue place for a seat you already hold is meaningless, and
        # promoting on it increments `enrolled_count` for a student who
        # was already counted -- losing the seat silently.
        #
        # `register` is what used to create this state and now clears it,
        # so on a correct build this branch is unreachable. It stays as a
        # BACKSTOP, and it repairs rather than merely skipping: the stale
        # entry is deleted, because it describes a queue place that cannot
        # ever be honoured. Then the loop continues, so the seat goes to a
        # candidate who can actually use it.
        #
        # Note the quota gate does NOT cover this. It shielded the bug in
        # the first reproduction attempt -- the candidate was at their cap
        # only BECAUSE the seat they already held counted toward it -- and
        # a candidate below their cap sails straight through. Nor does the
        # schedule check: `_conflicting_offering` excludes the target
        # offering itself, so a student's own enrollment in it is never a
        # clash. Two gates that look like they would catch this, neither
        # of which does.
        seated = db.execute(
            select(Enrollment).where(
                Enrollment.student_id == candidate.id,
                Enrollment.course_offering_id == offering.id,
                Enrollment.status == EnrollmentStatus.ACTIVE,
            )
        ).scalar_one_or_none()
        if seated is not None:
            log.warning(
                "waitlist: entry %s (student %s) on offering %s describes a "
                "seat the student already holds -- deleting stale entry",
                entry.id,
                entry.student_id,
                offering.id,
            )
            db.delete(entry)
            continue

        try:
            quotas.enforce_course_quota(db, candidate.id, candidate.role)
        except (quotas.QuotaExceeded, quotas.QuotaNotConfigured):
            # Skipped, not refused: there is no caller to tell. A missing
            # policy row fails closed here exactly as it does everywhere
            # else -- no policy is not the same as an unlimited one.
            log.info(
                "waitlist: skipped entry %s (student %s) on offering %s -- "
                "course-load quota",
                entry.id,
                entry.student_id,
                offering.id,
            )
            continue

        # The addition flagged in the docstring. Same lock, same class of
        # invariant, and without it promotion can create a timetable
        # clash that `register` would have refused.
        clash = _conflicting_offering(db, candidate.id, offering)
        if clash is not None:
            log.info(
                "waitlist: skipped entry %s (student %s) on offering %s -- "
                "clashes with offering %s",
                entry.id,
                entry.student_id,
                offering.id,
                clash.id,
            )
            continue

        # --- promote exactly one, then stop ----------------------------
        # `enrollment_unique` is UNCONDITIONAL, so a student who dropped
        # this offering earlier STILL OWNS A ROW. Promotion must UPDATE
        # it, never INSERT alongside it -- the same trap registration hit
        # at Deadline 4, and the one `check_waitlist.py` Part 1 pins down
        # before this code existed. A queued student who never enrolled
        # has no row, so both branches are reachable.
        existing = db.execute(
            select(Enrollment).where(
                Enrollment.student_id == candidate.id,
                Enrollment.course_offering_id == offering.id,
            )
        ).scalar_one_or_none()

        if existing is None:
            enrollment = Enrollment(
                student_id=candidate.id,
                course_offering_id=offering.id,
                status=EnrollmentStatus.ACTIVE,
            )
            db.add(enrollment)
        else:
            existing.status = EnrollmentStatus.ACTIVE
            enrollment = existing

        # The counter and the enrollment move together, in the same
        # transaction, always -- `enrolled_count` is derived state and
        # Deadline 8's reconciliation query exists to catch any path that
        # forgets. The caller decremented for the drop; this puts the seat
        # straight into the promoted student's hands.
        offering.enrolled_count += 1

        # DELETE, and nothing is renumbered: there is no `position`
        # column to renumber (dropped in revision c86676652ca2). Everyone
        # behind this entry moves up the next time a position is
        # COMPUTED. That is what makes a promotion a single-row write.
        db.delete(entry)

        log.info(
            "waitlist: promoted entry %s (student %s) into offering %s",
            entry.id,
            candidate.id,
            offering.id,
        )
        return enrollment

    # Nobody eligible. The seat stays free and the queue is untouched --
    # an ordinary registration may now take it, which is correct: every
    # queued student was either busy or ineligible at this instant.
    return None
