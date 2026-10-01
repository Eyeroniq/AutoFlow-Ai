import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.models.enums import ExecutionStatus, ExecutionTrigger, NodeExecutionStatus, pg_enum

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.workflow import Workflow, WorkflowNode


class WorkflowExecution(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "workflow_executions"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[ExecutionStatus] = mapped_column(
        pg_enum(ExecutionStatus, "execution_status"),
        default=ExecutionStatus.PENDING,
        server_default=ExecutionStatus.PENDING.value,
        index=True,
    )
    trigger: Mapped[ExecutionTrigger] = mapped_column(
        pg_enum(ExecutionTrigger, "execution_trigger"),
        default=ExecutionTrigger.MANUAL,
        server_default=ExecutionTrigger.MANUAL.value,
    )
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    triggered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    final_output_json: Mapped[dict[str, Any] | None]
    error_message: Mapped[str | None] = mapped_column(Text)

    # What the run was started with, so a worker runs exactly that even if the workflow is
    # edited while the execution waits in the queue.
    inputs_json: Mapped[dict[str, Any] | None]
    graph_json: Mapped[dict[str, Any] | None]
    # Dispatch bookkeeping: Celery queue + task id (the execution id), which worker claimed
    # it, its last heartbeat (stale => the worker died), and when a stop was requested.
    queue: Mapped[str | None] = mapped_column(String(100))
    celery_task_id: Mapped[str | None] = mapped_column(String(255))
    worker_hostname: Mapped[str | None] = mapped_column(String(255))
    heartbeat_at: Mapped[datetime | None]
    stop_requested_at: Mapped[datetime | None]
    # Queue hand-off: a run moves to another queue's workers when it reaches a node of that
    # kind (flowforge_engine.routing). `segment` counts the hand-offs; while the run waits
    # for a worker of the next queue it has no worker_hostname/heartbeat and handoff_at is
    # set. The next task claims segment N+1 with a compare-and-set, so it runs once.
    segment: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    handoff_at: Mapped[datetime | None]
    # Set for runs started through a deployment's endpoint (trigger "webhook").
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deployments.id", ondelete="SET NULL"), index=True
    )
    # The trigger that started the run (schedule, email, or webhook); its outcome feeds the
    # trigger's consecutive-failure count.
    trigger_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_triggers.id", ondelete="SET NULL"), index=True
    )

    workflow: Mapped["Workflow"] = relationship(back_populates="executions")
    triggered_by: Mapped["User | None"] = relationship()
    node_executions: Mapped[list["NodeExecution"]] = relationship(
        back_populates="execution", cascade="all, delete-orphan", passive_deletes=True
    )


class NodeExecution(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "node_executions"

    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="CASCADE"), index=True
    )
    # Nullable + SET NULL so deleting a node from the graph keeps its execution history;
    # the snapshot columns below keep that history readable once the node is gone.
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_nodes.id", ondelete="SET NULL"), index=True
    )
    node_key: Mapped[str] = mapped_column(String(100))
    # 0-based execution (topological) order, so not-yet-started nodes list in run order.
    position: Mapped[int | None] = mapped_column(Integer)
    node_type: Mapped[str] = mapped_column(String(100))
    node_label: Mapped[str] = mapped_column(String(255))
    status: Mapped[NodeExecutionStatus] = mapped_column(
        pg_enum(NodeExecutionStatus, "node_execution_status"),
        default=NodeExecutionStatus.PENDING,
        server_default=NodeExecutionStatus.PENDING.value,
    )
    input_json: Mapped[dict[str, Any] | None]
    output_json: Mapped[dict[str, Any] | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    # Which queue and worker ran the node (null for skipped nodes and sync runs' queue).
    queue: Mapped[str | None] = mapped_column(String(100))
    worker_hostname: Mapped[str | None] = mapped_column(String(255))
    # What was found in the step (counts and types) and what the privacy guard did; no values.
    privacy_json: Mapped[dict[str, Any] | None]

    execution: Mapped["WorkflowExecution"] = relationship(back_populates="node_executions")
    node: Mapped["WorkflowNode | None"] = relationship()
