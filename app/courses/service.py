from sqlalchemy.orm import Session

from app.models.course import Course, CourseOffering


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
