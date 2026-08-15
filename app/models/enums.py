import enum


class Role(str, enum.Enum):
    STUDENT = "STUDENT"
    FACULTY = "FACULTY"
    ADMIN = "ADMIN"


class ResourceType(str, enum.Enum):
    GPU = "GPU"
    ROOM = "ROOM"
    COURSE = "COURSE"


class ResourceStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    BLOCKED = "BLOCKED"


class ReservationStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"


class EnrollmentStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DROPPED = "DROPPED"
    WAITLISTED = "WAITLISTED"