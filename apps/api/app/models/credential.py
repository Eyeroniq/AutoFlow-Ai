import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


class Credential(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A user's secret for one provider (e.g. their Gemini key or Gmail App Password)."""

    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("user_id", "provider"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(100), index=True)
    # Fernet ciphertext of a JSON object (app.core.crypto); never stored or returned in clear.
    encrypted_value: Mapped[str] = mapped_column(Text)

    user: Mapped["User"] = relationship(back_populates="credentials")
