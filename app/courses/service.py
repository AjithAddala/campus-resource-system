from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.course import Course, CourseOffering
from app.models.enrollment import Enrollment
from app.models.enums import EnrollmentStatus
from app.models.user import User


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

    locked = db.execute(
        select(CourseOffering)
        .where(CourseOffering.id == offering_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()

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
    db.commit()
    db.refresh(enrollment)
    return enrollment
