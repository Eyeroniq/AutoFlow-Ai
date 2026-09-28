import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.enums import ExecutionStatus, ExecutionTrigger, NodeExecutionStatus


class RunRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"inputs": {"topic": "the history of workflow automation"}}]})

    inputs: dict[str, Any] = Field(
        default_factory=dict, description="Values for Input nodes, keyed by each Input node's `name`."
    )


def _duration_ms(started_at: datetime | None, finished_at: datetime | None) -> int | None:
    if started_at is None or finished_at is None:
        return None
    return round((finished_at - started_at).total_seconds() * 1000)


class NodeExecutionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    node_id: uuid.UUID | None = Field(description="Null once the node has been removed from the workflow.")
    node_key: str = Field(description="The node's id in the graph at execution time.")
    position: int | None = Field(default=None, description="Execution order (0-based).")
    node_type: str
    node_label: str
    status: NodeExecutionStatus
    input: dict[str, Any] | None = Field(validation_alias="input_json", description="Config after {{...}} resolution.")
    output: dict[str, Any] | None = Field(validation_alias="output_json")
    error_message: str | None = Field(description="Why the node failed, or why it was skipped.")
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None


class ExecutionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    status: ExecutionStatus
    trigger: ExecutionTrigger
    triggered_by_user_id: uuid.UUID | None
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    queue: str | None = Field(default=None, description="Celery queue the run was dispatched to (null for sync runs).")
    worker_hostname: str | None = Field(default=None, description="Worker that claimed the run.")
    heartbeat_at: datetime | None = Field(default=None, description="Last heartbeat from the worker while running.")
    stop_requested_at: datetime | None = None

    @computed_field
    @property
    def duration_ms(self) -> int | None:
        return _duration_ms(self.started_at, self.finished_at)


class ExecutionDetail(ExecutionSummary):
    inputs: dict[str, Any] | None = Field(default=None, validation_alias="inputs_json")
    final_output: dict[str, Any] | None = Field(validation_alias="final_output_json")
    node_executions: list[NodeExecutionRead]


class ExecutionLinks(BaseModel):
    execution: str = Field(description="GET: the execution with every node's state.")
    events: str = Field(description="WebSocket: live events (see README: Real-time events).")
    stop: str = Field(description="POST: stop the run.")


class ExecutionAccepted(BaseModel):
    """202 response of POST /run: the run is queued; follow it via `links`."""

    execution_id: uuid.UUID
    workflow_id: uuid.UUID
    status: ExecutionStatus
    queue: str
    links: ExecutionLinks

    @classmethod
    def build(cls, execution_id: uuid.UUID, workflow_id: uuid.UUID, status: ExecutionStatus, queue: str) -> "ExecutionAccepted":
        return cls(
            execution_id=execution_id,
            workflow_id=workflow_id,
            status=status,
            queue=queue,
            links=ExecutionLinks(
                execution=f"/api/executions/{execution_id}",
                events=f"/ws/executions/{execution_id}",
                stop=f"/api/executions/{execution_id}/stop",
            ),
        )
