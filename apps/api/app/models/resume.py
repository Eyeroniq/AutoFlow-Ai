import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin

# A refinement's status: queued, running its agents, done, or stopped by a failed agent.
REFINEMENT_STATUSES = ("pending", "running", "success", "failed")


class ResumeRefinement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One run of the multi-agent resume pipeline (app.services.resume_refinement) on one PDF.

    `stages_json` holds every agent's stage as it ran: status, provider and model used, every raw
    model reply (and the problems that made a reply be retried), the validated output, timing.
    `result_json` is the assembled final result (the rewritten draft, before/after per bullet,
    statistics). Refining the same file again creates a new row with the next `version`.
    """

    __tablename__ = "resume_refinements"

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id", ondelete="SET NULL"))
    filename: Mapped[str] = mapped_column(String(255))
    # SHA-256 of the PDF: the same file refined again is the next version of the same resume.
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    job_description: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", index=True)
    stages_json: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    result_json: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class ResumeEmail(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A refined resume emailed to its owner: who, when (`created_at`), which version, and what was said."""

    __tablename__ = "resume_emails"

    refinement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resume_refinements.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Always the account's own address at the time of sending.
    to_email: Mapped[str] = mapped_column(String(320))
    version: Mapped[int] = mapped_column(Integer)
    subject: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text)
    attachments_json: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    message_id: Mapped[str | None] = mapped_column(String(255), default=None)
