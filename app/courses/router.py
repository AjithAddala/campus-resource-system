from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user, require_role
from app.core.errors import coded_error
from app.courses import service
from app.courses.schemas import (
    CourseOfferingRead,
    CourseRead,
    EnrollmentRead,
    WaitlistEntryRead,
)
from app.database.session import get_db
from app.models.enums import Role
from app.models.user import User
from app.quotas import service as quotas

# Two routers, deliberately. Reads stay course-shaped because browsing a
# catalogue genuinely is; write paths are offering-shaped because the
# offering is the row holding enrolled_count and therefore the row that
# gets locked. See DECISIONS.md, "Course write paths are keyed on the
# offering". Deadline 4 adds POST /offerings/{id}/register to the second
# router below, not to the first.
router = APIRouter(prefix="/courses", tags=["courses"])
offerings_router = APIRouter(prefix="/offerings", tags=["offerings"])


@router.get("", response_model=list[CourseRead])
def list_courses(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[CourseRead]:
    return service.list_courses(db)


@router.get("/{course_id}", response_model=CourseRead)
def get_course(
    course_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> CourseRead:
    course = service.get_course(db, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")
    return course


@router.get("/{course_id}/offerings", response_model=list[CourseOfferingRead])
def list_course_offerings(
    course_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[CourseOfferingRead]:
    # 404 on the parent rather than returning [] for a course that does
    # not exist — an empty list means "no sections this semester", which
    # is a different answer.
    if service.get_course(db, course_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")
    return service.list_offerings(db, course_id)


@offerings_router.get("/{offering_id}", response_model=CourseOfferingRead)
def get_offering(
    offering_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> CourseOfferingRead:
    offering = service.get_offering(db, offering_id)
    if offering is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offering not found")
    return offering


@offerings_router.post(
    "/{offering_id}/register",
    response_model=EnrollmentRead,
    status_code=status.HTTP_201_CREATED,
)
def register_for_offering(
    offering_id: int,
    db: Session = Depends(get_db),
    student: User = Depends(require_role(Role.STUDENT)),
) -> EnrollmentRead:
    """Take a seat. **STUDENT only** — a FACULTY or ADMIN token gets 403.

    That restriction is why `scripts/seed.py` deliberately has no
    `(FACULTY, COURSE)` quota row: the pair is unreachable behind this
    dependency, so "no row" and "row with max_units = NULL" stay two
    different things for the quota helper at Deadline 6.

    **The route is keyed on the OFFERING, not the course**, and that is
    the whole mechanism rather than a naming preference. Capacity,
    `enrolled_count` and the locked row all live on `course_offerings`,
    and one course has many offerings — so `/courses/{id}/register` would
    have no single row to lock. See DECISIONS.md.

    No request body: the seat is identified entirely by the path and the
    token. There is nothing for a caller to get wrong, and nothing to
    validate.

    Four 409s, distinguishable by code because the remedies differ:

        ALREADY_ENROLLED     you already hold this seat  -> nothing to do
        QUOTA_EXCEEDED       your course load is full    -> drop any one
        SCHEDULE_CONFLICT    it clashes with another     -> drop that one
        CAPACITY_EXHAUSTED   the section is full         -> wait / another

    The order they are checked in is the service's, and it is deliberate;
    `service.register` explains why quota comes after ALREADY_ENROLLED
    and before the other two.
    """
    try:
        enrollment = service.register(db, offering_id, student)
    except service.AlreadyEnrolled:
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "ALREADY_ENROLLED",
            "You are already enrolled in this offering.",
        )
    except quotas.QuotaExceeded as exc:
        # Deadline 6. `QUOTA_EXCEEDED` rather than a course-specific code:
        # it is the same invariant the GPU and room paths enforce, on a
        # third resource, and the caller's remedy is the same shape --
        # release something you hold. `exc.resource_type` names which one.
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "QUOTA_EXCEEDED",
            f"You hold {exc.held} of {exc.limit} permitted course enrollments.",
        )
    except quotas.QuotaNotConfigured as exc:
        # Reachable in principle and not in practice: registration is
        # STUDENT-only and (STUDENT, COURSE) is seeded. It fails closed
        # anyway, because a missing policy row is no policy at all.
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "QUOTA_NOT_CONFIGURED",
            f"No quota policy is configured for role {exc.role.value} "
            f"and resource {exc.resource_type.value}.",
        )
    except service.ScheduleConflict as exc:
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "SCHEDULE_CONFLICT",
            f"This offering meets at the same time as offering {exc.other.id} "
            f"({exc.other.days} {exc.other.start_time}-{exc.other.end_time}).",
        )
    except service.OfferingFull as exc:
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "CAPACITY_EXHAUSTED",
            f"All {exc.capacity} seats are taken.",
        )

    if enrollment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offering not found")

    return enrollment


@offerings_router.delete("/{offering_id}/drop", response_model=EnrollmentRead)
def drop_offering(
    offering_id: int,
    db: Session = Depends(get_db),
    student: User = Depends(require_role(Role.STUDENT)),
) -> EnrollmentRead:
    """Release a seat. STUDENT only.

    Not listed in the plan's Deadline 4 column, and it belongs here for a
    concrete reason: the column requires that "re-registration is an
    UPDATE, not an INSERT", and that is untestable without a DROPPED row
    to re-register over. It is the fifth endpoint found specified (in
    INIT_PLAN.md §12) but assigned to no deadline.

    Returns the row rather than 204, so a repeated drop can answer with
    the same body instead of an error — the same shape as GPU cancel.
    """
    try:
        enrollment = service.drop(db, offering_id, student)
    except service.NotEnrolled:
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "NOT_ENROLLED",
            "You have no enrollment in this offering.",
        )

    if enrollment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offering not found")

    return enrollment


# ---------------------------------------------------------------------------
# Waitlist — Deadline 7, B's column
# ---------------------------------------------------------------------------
#
# Three routes on ONE path, `/offerings/{id}/waitlist`, distinguished by
# method: POST to join, DELETE to leave, GET to read the queue. The path
# is offering-shaped for the same reason `register` is -- the offering is
# the row that gets locked, and a course has many offerings, so
# `/courses/{id}/waitlist` would name no single queue.
#
# Item 10, ratified: joining is EXPLICIT. There is deliberately no
# fall-through from a full `POST /register`, which keeps
# `409 CAPACITY_EXHAUSTED` meaning exactly one thing.


def _as_read(entry, position: int) -> WaitlistEntryRead:
    """Attach the read-time position to a row that does not carry one.

    `from_attributes` cannot do this on its own: `position` is not an
    attribute of `WaitlistEntry` and never will be. Built in one place so
    the three routes below cannot disagree about the shape.
    """
    return WaitlistEntryRead(
        id=entry.id,
        student_id=entry.student_id,
        course_offering_id=entry.course_offering_id,
        created_at=entry.created_at,
        position=position,
    )


@offerings_router.post(
    "/{offering_id}/waitlist",
    response_model=WaitlistEntryRead,
    status_code=status.HTTP_201_CREATED,
)
def join_waitlist(
    offering_id: int,
    db: Session = Depends(get_db),
    student: User = Depends(require_role(Role.STUDENT)),
) -> WaitlistEntryRead:
    """Queue for a seat in a FULL offering. **STUDENT only** — else 403.

    Same role gate as `register`, and for the same reason: a waitlist
    entry is a claim on a seat, and only students take seats. It is also
    what keeps `(FACULTY, COURSE)` unreachable, which `scripts/seed.py`
    relies on to keep "no quota row" and "row with `max_units = NULL`"
    two distinguishable states.

    `201` with the entry and its position. Three 409s, distinguishable by
    code because the remedies differ:

        ALREADY_ENROLLED     you already hold a seat here -> nothing to do
        ALREADY_WAITLISTED   you are already queued       -> nothing to do
        OFFERING_NOT_FULL    there are seats left         -> register instead

    `OFFERING_NOT_FULL` is new at this deadline and is the one that could
    have gone either way. Accepting the join silently would leave the
    student queued behind a seat they could have taken, waiting for a
    promotion that only ever fires on a DROP.

    **No `QUOTA_EXCEEDED` here.** Queueing holds nothing, so it costs no
    course-load quota; the quota is enforced when the promotion tries to
    seat the student, where A's transaction skips a candidate who would
    breach it. Ratified with item 10.
    """
    try:
        result = service.join_waitlist(db, offering_id, student)
    except service.AlreadyEnrolled:
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "ALREADY_ENROLLED",
            "You already hold a seat in this offering.",
        )
    except service.AlreadyWaitlisted:
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "ALREADY_WAITLISTED",
            "You are already on the waitlist for this offering.",
        )
    except service.OfferingNotFull as exc:
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "OFFERING_NOT_FULL",
            f"{exc.capacity - exc.enrolled} of {exc.capacity} seats are still "
            "available; register instead of joining the waitlist.",
        )

    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offering not found")

    entry, position = result
    return _as_read(entry, position)


@offerings_router.delete(
    "/{offering_id}/waitlist", response_model=WaitlistEntryRead
)
def leave_waitlist(
    offering_id: int,
    db: Session = Depends(get_db),
    student: User = Depends(require_role(Role.STUDENT)),
) -> WaitlistEntryRead:
    """Give up a place in the queue. STUDENT only.

    Returns the entry that was removed rather than `204`, matching `drop`
    and GPU cancel: the caller is owed what they gave up.

    **`position` on this response is the position the student HELD**, read
    before the delete. It is the last true statement about an entry that
    no longer exists, and it is deliberately not recomputed afterwards.

    `409 NOT_WAITLISTED` when there is nothing to leave — mirroring
    `NOT_ENROLLED` on `drop`. Not naturally idempotent the way `drop` is,
    and the difference is the schema's rather than a choice: a dropped
    enrollment keeps its row and can answer a repeated call with the same
    body, while a left waitlist entry is gone and a second call has
    nothing to return.
    """
    entries = service.list_waitlist(db, offering_id)
    if entries is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offering not found")

    # Read BEFORE the delete, because afterwards there is no position to
    # report. Unlocked and therefore advisory -- consistent with every
    # other position this API hands out.
    held = next((p for e, p in entries if e.student_id == student.id), 0)

    try:
        entry = service.leave_waitlist(db, offering_id, student)
    except service.NotWaitlisted:
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "NOT_WAITLISTED",
            "You are not on the waitlist for this offering.",
        )

    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offering not found")

    return _as_read(entry, held)


@offerings_router.get(
    "/{offering_id}/waitlist", response_model=list[WaitlistEntryRead]
)
def get_waitlist(
    offering_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[WaitlistEntryRead]:
    """The queue, oldest first, with positions computed at read time.

    Any authenticated role may read it, like the rest of the catalogue —
    a roster is not privileged information in this system, and the role
    matrix gates *taking* resources rather than *seeing* them.

    **Takes no lock**, the same rule `GET /me/quota` follows. The numbers
    are a display value: a caller reading position 3 may be position 2 by
    the time they act on it, and that is fine, because the promotion
    transaction recomputes the order under the offering lock and is what
    has to be right. Locking here would serialize reading against
    promoting to produce a number that goes stale anyway.

    `404` for an offering that does not exist, rather than `[]` — an
    empty list means "nobody is queued", which is a different answer.
    """
    entries = service.list_waitlist(db, offering_id)
    if entries is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offering not found")
    return [_as_read(entry, position) for entry, position in entries]
