import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import TriggerType, pg_enum

if TYPE_CHECKING:
    from app.models.workflow import Workflow


class WorkflowTrigger(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """What starts a workflow without a click: a schedule, new email, or its webhook.

    At most one of each type per workflow. `next_fire_at` is when it is next due (the next
    cron time for a schedule, the next mailbox poll for email; unused for webhooks), and it
    is claimed with a compare-and-set, so a due time fires once however many ticks see it.
    After `workflows.max_consecutive_failures` failed runs in a row the trigger switches
    itself off (`auto_disabled_at`, `disabled_reason`) until someone re-enables it.
    """

    __tablename__ = "workflow_triggers"
    __table_args__ = (UniqueConstraint("workflow_id", "type"),)

    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    type: Mapped[TriggerType] = mapped_column(pg_enum(TriggerType, "trigger_type"))
    enabled: Mapped[bool] = mapped_column(default=False, server_default="false")
    # schedule: {cron, timezone, inputs}; email: {folder, from_address, subject, ...}.
    config_json: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    # email: {uidvalidity, last_uid}, the mailbox position already looked at.
    state_json: Mapped[dict[str, Any] | None]
    next_fire_at: Mapped[datetime | None] = mapped_column(index=True)
    last_fired_at: Mapped[datetime | None]
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    auto_disabled_at: Mapped[datetime | None]
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    # The latest problem that didn't produce a run (skipped by the hourly limit, a mailbox
    # that couldn't be read, a missed schedule), for the Triggers panel.
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None]

    workflow: Mapped["Workflow"] = relationship(back_populates="triggers")


class TriggerEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One occurrence a trigger acted on: a schedule's fire time or an email's Message-ID.

    The unique (trigger_id, event_key) pair is what makes firing idempotent: a second tick,
    a redelivered task, or a repeated poll inserts nothing and so starts nothing.
    """

    __tablename__ = "trigger_events"
    __table_args__ = (UniqueConstraint("trigger_id", "event_key"),)

    trigger_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_triggers.id", ondelete="CASCADE"), index=True)
    event_key: Mapped[str] = mapped_column(String(998))
    # started | skipped (hourly limit) | rejected (the workflow didn't validate) | missed
    outcome: Mapped[str] = mapped_column(String(32))
    execution_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="SET NULL"), index=True
    )
    detail: Mapped[str | None] = mapped_column(Text)


class NodeState(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """State a node keeps between runs (flowforge_engine.state), e.g. the RSS entries it has
    returned. One row per run that saved state; a node reads the newest row whose execution
    succeeded, so a failed run doesn't move it forward."""

    __tablename__ = "node_states"
    __table_args__ = (Index("ix_node_states_workflow_node", "workflow_id", "node_key", "created_at"),)

    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    node_key: Mapped[str] = mapped_column(String(100))
    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="CASCADE"), index=True
    )
    state_json: Mapped[dict[str, Any]]
