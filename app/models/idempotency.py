from datetime import datetime

from sqlalchemy import DateTime, String, Integer, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class IdempotencyKey(Base):
    """Exactly-once guarantee. The UNIQUE(key, user_id) insert is the
    serialization point for retries — see canonical GPU transaction.
    """
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("key", "user_id", name="idempotency_key_user_unique"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # detects key reuse w/ different body
    response_body: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )