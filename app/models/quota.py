from sqlalchemy import Integer, Enum, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.enums import Role, ResourceType


class RoleQuota(Base):
    """Admin-editable policy: max concurrently-held units per (role, resource_type).

    max_units NULL = unlimited (used for ADMIN).
    """
    __tablename__ = "role_quotas"
    __table_args__ = (
        UniqueConstraint("role", "resource_type", name="role_quota_unique"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[Role] = mapped_column(Enum(Role, name="role_enum"), nullable=False)
    resource_type: Mapped[ResourceType] = mapped_column(
        Enum(ResourceType, name="resource_type_enum"), nullable=False
    )
    max_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
