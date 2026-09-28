import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKeyMixin
from app.models.enums import IntegrationStatus, pg_enum

if TYPE_CHECKING:
    from app.models.user import User


class Integration(UUIDPrimaryKeyMixin, Base):
    """Connection status for a user's provider credential, plus non-secret metadata
    (masked key, last connection test)."""

    __tablename__ = "integrations"
    __table_args__ = (UniqueConstraint("user_id", "provider"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[IntegrationStatus] = mapped_column(
        pg_enum(IntegrationStatus, "integration_status"),
        default=IntegrationStatus.DISCONNECTED,
        server_default=IntegrationStatus.DISCONNECTED.value,
    )
    connected_at: Mapped[datetime | None]
    metadata_json: Mapped[dict[str, Any] | None]

    user: Mapped["User"] = relationship(back_populates="integrations")
