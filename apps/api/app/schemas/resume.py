import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RefinementCreate(BaseModel):
    file_id: uuid.UUID = Field(description="A PDF uploaded with POST /api/files.")
    job_description: str = Field(default="", max_length=30_000, description="Optional: runs the Job-Match Agent and tailors the rewrite.")


class StageRead(BaseModel):
    """One pipeline stage: an agent's status, the model that answered, every raw reply, and its validated output."""

    model_config = ConfigDict(extra="allow")

    key: str
    title: str
    role: str
    status: str
    note: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    provider_used: str | None = None
    model: str | None = None
    mock: bool | None = None
    attempts: list[dict[str, Any]] = []
    fallback_errors: list[dict[str, Any]] = []
    prompt: str | None = None
    output: dict[str, Any] | None = None
    error: str | None = None


class EmailRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    refinement_id: uuid.UUID
    to_email: str
    version: int
    subject: str
    summary: str
    attachments_json: dict[str, Any]
    created_at: datetime


class RefinementSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    version: int
    status: str
    has_job_description: bool
    created_at: datetime
    finished_at: datetime | None = None
    error_message: str | None = None
    emails_sent: int = 0


class RefinementRead(RefinementSummary):
    job_description: str
    stages: list[StageRead]
    result: dict[str, Any] | None = None
    emails: list[EmailRead] = []
