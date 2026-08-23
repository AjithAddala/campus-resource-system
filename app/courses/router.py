from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user, require_role
from app.core.errors import coded_error
from app.courses import service
from app.courses.schemas import CourseOfferingRead, CourseRead, EnrollmentRead
from app.database.session import get_db
from app.models.enums import Role
from app.models.user import User

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

    Three 409s, distinguishable by code because the remedies differ:

        ALREADY_ENROLLED     you already hold this seat  -> nothing to do
        SCHEDULE_CONFLICT    it clashes with another      -> drop that one
        CAPACITY_EXHAUSTED   the section is full          -> wait / another
    """
    try:
        enrollment = service.register(db, offering_id, student)
    except service.AlreadyEnrolled:
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "ALREADY_ENROLLED",
            "You are already enrolled in this offering.",
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
