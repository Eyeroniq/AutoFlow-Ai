import uuid

from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class UploadedFile(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A file a user uploaded (POST /api/files). The bytes live on the upload volume shared
    by the API and the workers, at FILES_DIR/<storage_key>; only the owner can read them."""

    __tablename__ = "files"

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # The uploaded name, sanitized (no path parts or control characters); display only.
    filename: Mapped[str] = mapped_column(String(255))
    # Detected from the file's content, not taken from the client.
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    # "<owner id>/<file id>", relative to FILES_DIR; never derived from the filename.
    storage_key: Mapped[str] = mapped_column(String(255))
