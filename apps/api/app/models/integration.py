import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKeyMixin
from app.models.enums import IntegrationStatus, pg_enum

if TYPE_CHECKING:
    from app.models.user import User


class Integration(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "integrations"

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
