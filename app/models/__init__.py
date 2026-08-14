"""SQLAlchemy ORM models.

Import every model here so that `Base.metadata` is fully populated
whenever `app.models` is imported (Alembic autogenerate + app startup
both rely on this).
"""
from app.models.enums import Role, ResourceType, ResourceStatus, ReservationStatus, EnrollmentStatus
from app.models.user import User
from app.models.quota import RoleQuota
from app.models.idempotency import IdempotencyKey
from app.models.resource import Resource, Room, GPUCluster
from app.models.course import Course, CourseOffering
from app.models.reservation import Reservation, GPUReservation
from app.models.enrollment import Enrollment, WaitlistEntry

__all__ = [
    "Role", "ResourceType", "ResourceStatus", "ReservationStatus", "EnrollmentStatus",
    "User", "RoleQuota", "IdempotencyKey",
    "Resource", "Room", "GPUCluster",
    "Course", "CourseOffering",
    "Reservation", "GPUReservation",
    "Enrollment", "WaitlistEntry",
]