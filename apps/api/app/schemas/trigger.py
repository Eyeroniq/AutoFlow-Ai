import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ExecutionStatus, TriggerType


class TriggerLastRun(BaseModel):
    execution_id: uuid.UUID
    status: ExecutionStatus
    created_at: datetime
    finished_at: datetime | None
    error_message: str | None


class WebhookInfo(BaseModel):
    deployment_id: uuid.UUID
    endpoint: str = Field(description="POST it with the deployment's API key: Authorization: Bearer <key>.")
    api_key_prefix: str
    deployed_version: int
    behind: bool = Field(description="The saved workflow has edits the deployment doesn't serve yet.")


class TriggerRead(BaseModel):
    type: TriggerType
    id: uuid.UUID | None = Field(description="Null until the trigger is first saved.")
    configured: bool
    enabled: bool
    config: dict[str, Any]
    next_run_at: datetime | None = Field(description="Schedule: the next fire time. Email: the next mailbox check.")
    upcoming: list[datetime] = Field(default_factory=list, description="Schedule: the next few fire times.")
    last_fired_at: datetime | None = None
    last_run: TriggerLastRun | None = None
    consecutive_failures: int = 0
    auto_disabled_at: datetime | None = Field(default=None, description="Set when the failure limit switched it off.")
    disabled_reason: str | None = None
    last_error: str | None = Field(default=None, description="The latest problem that didn't produce a run (e.g. skipped by the hourly limit).")
    last_error_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)
    webhook: WebhookInfo | None = None
    mailbox: dict[str, Any] | None = Field(default=None, description="Email: the folder position seen and the last check.")


class TriggerSettings(BaseModel):
    max_runs_per_hour: int = Field(ge=1, le=1000, description="Triggered runs (schedule, email, webhook) per rolling hour.")
    max_consecutive_failures: int = Field(
        ge=0, le=100, description="A trigger switches itself off after this many failed runs in a row (0 = never)."
    )


class TriggersRead(BaseModel):
    workflow_id: uuid.UUID
    settings: TriggerSettings
    runs_last_hour: int
    triggers: list[TriggerRead]


class TriggerUpdate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"enabled": True, "config": {"cron": "30 7 * * *", "timezone": "Asia/Kolkata"}},
                {"enabled": True, "config": {"subject": "invoice", "poll_minutes": 5, "input_name": "email"}},
                {"enabled": False},
            ]
        }
    )

    enabled: bool
    config: dict[str, Any] | None = Field(
        default=None,
        description=(
            "schedule: {cron, timezone, inputs}. email: {folder, from_address, subject, unread_only, poll_minutes, "
            "input_name, max_per_poll, mark_as_read, max_body_chars}. webhook: {}. Omit to keep the current config."
        ),
    )


class SchedulePreviewRequest(BaseModel):
    cron: str
    timezone: str = "UTC"
    count: int = Field(default=5, ge=1, le=20)


class SchedulePreview(BaseModel):
    valid: bool
    error: str | None = None
    cron: str | None = Field(default=None, description="Normalized (aliases like @daily expanded).")
    interval: bool | None = Field(default=None, description="True: follows real time across DST changes.")
    next: list[datetime] = Field(default_factory=list)


class TriggerBrief(BaseModel):
    type: TriggerType
    enabled: bool
    auto_disabled: bool
