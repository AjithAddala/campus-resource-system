from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.courses import service
from app.courses.schemas import CourseOfferingRead, CourseRead
from app.database.session import get_db
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
