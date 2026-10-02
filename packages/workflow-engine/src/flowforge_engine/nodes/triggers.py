"""Trigger blocks: what starts a pipeline, as nodes on the canvas.

Schedule Trigger, Email Trigger and Telegram Trigger hold the trigger's settings. Saving the pipeline turns each
block into the real trigger (app.services.triggers.sync_trigger_blocks): the scheduler, the mailbox poller and the
Telegram Command Center read their settings from it, and the block's `enabled` switch turns it on or off. When the
pipeline runs, a block just reports its settings; the data a trigger passes in arrives through the pipeline's Input
nodes as it always has (a schedule's `inputs`, the email as the Input named `input_name`, a Telegram message's
extracted values).

The settings are validated when the pipeline is saved (a bad cron expression, a chat id that isn't a number...);
the reason is shown on the trigger and the trigger stays off.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from flowforge_engine.models import IDENTIFIER_PATTERN, NodeContext, NodeResult
from flowforge_engine.registry import NodeConfig, NodeDefinition, register_node


class ScheduleTriggerConfig(NodeConfig):
    enabled: bool = Field(default=False, description="Switch the schedule on. Off by default so nothing runs by surprise.")
    cron: str = Field(
        default="30 7 * * *", min_length=1, max_length=100,
        description="5-field cron, e.g. '30 7 * * *' (07:30 daily) or '*/15 * * * *'.",
    )
    timezone: str = Field(default="UTC", max_length=64, description="IANA time zone the cron is read in, e.g. Asia/Kolkata.")
    inputs: dict[str, Any] = Field(default_factory=dict, description="Values for the pipeline's Input nodes on each run, by input name.")


class ScheduleTriggerResult(BaseModel):
    source: str
    cron: str
    timezone: str
    fired_at: str


@register_node("schedule_trigger")
class ScheduleTriggerNode(NodeDefinition[ScheduleTriggerConfig]):
    category = "sources"
    label = "Schedule Trigger"
    description = "Runs this pipeline on a cron schedule in any time zone (switch it on with Enabled, then save)."
    icon = "timer"
    config_schema = ScheduleTriggerConfig
    output_schema = ScheduleTriggerResult

    async def execute(self, context: NodeContext, config: ScheduleTriggerConfig) -> NodeResult:
        return NodeResult.ok(
            source="schedule", cron=config.cron, timezone=config.timezone, fired_at=datetime.now(UTC).isoformat(),
        )


class EmailTriggerConfig(NodeConfig):
    enabled: bool = Field(default=False, description="Switch the mailbox check on. Only mail arriving afterwards starts runs.")
    folder: str = Field(default="INBOX", min_length=1, max_length=255)
    from_address: str | None = Field(default=None, max_length=255, description="Only mail whose From contains this.")
    subject: str | None = Field(default=None, max_length=255, description="Only mail whose Subject contains this.")
    unread_only: bool = True
    poll_minutes: int = Field(default=5, ge=1, le=1440, description="How often the mailbox is checked.")
    input_name: str = Field(
        default="email", max_length=100, pattern=IDENTIFIER_PATTERN,
        description="The pipeline Input (type JSON or text) the email is passed to.",
    )
    max_per_poll: int = Field(default=10, ge=1, le=50)
    mark_as_read: bool = False
    max_body_chars: int = Field(default=5000, ge=0, le=100_000)


class EmailTriggerResult(BaseModel):
    source: str
    folder: str
    input_name: str


@register_node("email_trigger")
class EmailTriggerNode(NodeDefinition[EmailTriggerConfig]):
    category = "sources"
    label = "Email Trigger"
    description = "Runs this pipeline for each new Gmail message (one run per message). Needs a Gmail connection."
    icon = "log-in"
    config_schema = EmailTriggerConfig
    output_schema = EmailTriggerResult

    async def execute(self, context: NodeContext, config: EmailTriggerConfig) -> NodeResult:
        return NodeResult.ok(source="email", folder=config.folder, input_name=config.input_name)


class TelegramTriggerConfig(NodeConfig):
    enabled: bool = Field(
        default=False,
        description="Let messages from the chats below run this pipeline. It must be deployed (with a description) first.",
    )
    allowed_chat_ids: str = Field(
        default="", max_length=1000,
        description="Comma-separated chat ids that may run it. Empty: TELEGRAM_CHAT_ID from the server's .env.",
    )


class TelegramTriggerResult(BaseModel):
    source: str
    allowed_chat_ids: str


@register_node("telegram_trigger")
class TelegramTriggerNode(NodeDefinition[TelegramTriggerConfig]):
    category = "sources"
    label = "Telegram Trigger"
    description = (
        "Lets you run this deployed pipeline by messaging your Telegram bot: an AI router picks the pipeline and fills its inputs."
    )
    icon = "message-square"
    config_schema = TelegramTriggerConfig
    output_schema = TelegramTriggerResult

    async def execute(self, context: NodeContext, config: TelegramTriggerConfig) -> NodeResult:
        return NodeResult.ok(source="telegram", allowed_chat_ids=config.allowed_chat_ids)
