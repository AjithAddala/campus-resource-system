import logging

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.courses.schemas import DAY_CODES, CourseCreate, CourseOfferingCreate
from app.models.course import Course, CourseOffering
from app.models.enrollment import Enrollment, WaitlistEntry
from app.models.enums import EnrollmentStatus, Role
from app.models.user import User
from app.quotas import service as quotas

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


class CourseCodeTaken(Exception):
    """`courses.code` is unique and this one is spoken for."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"course code {code} already exists")


class CourseNotFound(Exception):
    """`course_id` names no catalogue row."""


class InstructorNotFound(Exception):
    """`instructor_id` names no user."""


class InstructorNotFaculty(Exception):
    """The named user exists and is not FACULTY."""

    def __init__(self, role: Role):
        self.role = role
        super().__init__(f"user is {role.value}, not FACULTY")


def create_course(db: Session, payload: CourseCreate) -> Course:
    """Add a catalogue entry. Raises `CourseCodeTaken` on a duplicate code.

    **The duplicate is caught, not pre-checked**, for the reason Deadline 2
    settled on duplicate registration: two simultaneous creates can both
    pass a "does this code exist?" read and only one can insert, so a
    pre-check turns a 409 into a 500 exactly when two admins race. The
    unique index is the guarantee; this is the translation.

    The constraint name is read from psycopg's diagnostics rather than
    matched against message text, the same discrimination
    `rooms/service.py::_is_overlap_violation` makes -- mapping every
    IntegrityError to "code taken" would report the next unrelated
    constraint failure as a duplicate.

    **The name is `ix_courses_code`, not `courses_code_key`.** The model
    declares `unique=True, index=True`, and that pair makes SQLAlchemy
    emit a unique INDEX rather than a unique CONSTRAINT -- so Postgres
    reports the index's name and the constraint-shaped guess matches
    nothing. Verified against `pg_indexes`, and asserted by
    `check_catalog.py`: had it been left wrong, the duplicate would have
    surfaced as an uncaught IntegrityError and a 500.
    """
    course = Course(code=payload.code, name=payload.name)
    db.add(course)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        diag = getattr(getattr(exc, "orig", None), "diag", None)
        if getattr(diag, "constraint_name", None) == "ix_courses_code":
            raise CourseCodeTaken(payload.code) from exc
        raise
    db.refresh(course)
    return course


def create_offering(db: Session, payload: CourseOfferingCreate) -> CourseOffering:
    """Open one section of an existing course.

    Raises `CourseNotFound`, `InstructorNotFound` or `InstructorNotFaculty`.

    Both foreign keys are resolved explicitly rather than left to the FK
    constraint, because the constraint cannot tell the two of them apart:
    one `ForeignKeyViolation` for `course_id` and `instructor_id` alike,
    and the caller needs to know which id was wrong. The FK still stands
    behind this -- a row deleted between the read and the insert fails at
    the database, which is the correct place for a race this endpoint has
    no reason to serialize.

    `enrolled_count` is set to 0 explicitly. It is the counter that
    `register` locks and mutates, and the only moment in its life when it
    is safe to write outside that transaction is before the row exists.

    **No instructor double-booking check, and that is a decision.** Two
    sections meeting at the same hour with the same instructor is a real
    integrity problem, and `_days_overlap`/`_times_overlap` are right here
    -- but checking it correctly means locking the instructor's row for
    the duration, and checking it *incorrectly* means an unlocked boundary
    read of exactly the kind §7 and Workflow B spend pages refusing. A
    gate that two concurrent admins can walk straight through would read
    as a guarantee while being none. Left out, stated in the README.
    """
    if get_course(db, payload.course_id) is None:
        raise CourseNotFound(f"no course with id {payload.course_id}")

    instructor = db.get(User, payload.instructor_id)
    if instructor is None:
        raise InstructorNotFound(f"no user with id {payload.instructor_id}")
    if instructor.role is not Role.FACULTY:
        raise InstructorNotFaculty(instructor.role)

    offering = CourseOffering(
        course_id=payload.course_id,
        instructor_id=payload.instructor_id,
        semester=payload.semester,
        year=payload.year,
        start_time=payload.start_time,
        end_time=payload.end_time,
        days=payload.days,
        capacity=payload.capacity,
        enrolled_count=0,
    )
    db.add(offering)
    db.commit()
    db.refresh(offering)
    return offering


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


__all__ = ["DAY_CODES"]


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

    db.execute(select(User.id).where(User.id == student.id).with_for_update())

    existing = db.execute(
        select(Enrollment).where(
            Enrollment.student_id == student.id,
            Enrollment.course_offering_id == offering_id,
        )
    ).scalar_one_or_none()

    if existing is not None and existing.status is EnrollmentStatus.ACTIVE:
        raise AlreadyEnrolled(offering_id)

    quotas.enforce_course_quota(db, student.id, student.role)

    conflict = _conflicting_offering(db, student.id, offering)
    if conflict is not None:
        raise ScheduleConflict(conflict)

    seat_read = (
        select(CourseOffering)
        .where(CourseOffering.id == offering_id)
        .execution_options(populate_existing=True)
    )
    if not get_settings().BENCHMARK_UNSAFE_NO_OFFERING_LOCK:
        seat_read = seat_read.with_for_update()
    locked = db.execute(seat_read).scalar_one()

    if locked.enrolled_count >= locked.capacity:
        raise OfferingFull(locked.capacity, locked.enrolled_count)

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

    _promote_one(db, locked)

    db.commit()
    db.refresh(enrollment)
    return enrollment


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

    db.execute(select(User.id).where(User.id == student.id).with_for_update())

    enrollment = db.execute(
        select(Enrollment).where(
            Enrollment.student_id == student.id,
            Enrollment.course_offering_id == offering_id,
        )
    ).scalar_one_or_none()
    if enrollment is not None and enrollment.status is EnrollmentStatus.ACTIVE:
        raise AlreadyEnrolled(offering_id)

    locked = db.execute(
        select(CourseOffering)
        .where(CourseOffering.id == offering_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).scalar_one()

    if locked.enrolled_count < locked.capacity:
        raise OfferingNotFull(locked.capacity, locked.enrolled_count)

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

    left = WaitlistEntry(
        id=entry.id,
        student_id=entry.student_id,
        course_offering_id=entry.course_offering_id,
        created_at=entry.created_at,
    )

    db.delete(entry)
    db.commit()

    return left


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
    if offering.enrolled_count >= offering.capacity:
        return None

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
        acquired = db.execute(
            select(User.id)
            .where(User.id == entry.student_id)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()

        if acquired is None:
            log.info(
                "waitlist: skipped entry %s (student %s) on offering %s -- "
                "user row busy",
                entry.id,
                entry.student_id,
                offering.id,
            )
            continue

        candidate = db.execute(
            select(User)
            .where(User.id == entry.student_id)
            .execution_options(populate_existing=True)
        ).scalar_one()

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
            log.info(
                "waitlist: skipped entry %s (student %s) on offering %s -- "
                "course-load quota",
                entry.id,
                entry.student_id,
                offering.id,
            )
            continue

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

        offering.enrolled_count += 1

        db.delete(entry)

        log.info(
            "waitlist: promoted entry %s (student %s) into offering %s",
            entry.id,
            candidate.id,
            offering.id,
        )
        return enrollment

    return None
