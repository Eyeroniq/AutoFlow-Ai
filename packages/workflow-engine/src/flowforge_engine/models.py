"""Data models shared by the registry, validator, and executor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from flowforge_engine.services import ExecutionServices

# Graph ids appear inside {{...}} references, so they can't contain '.', brackets, braces,
# or whitespace.
IDENTIFIER_PATTERN = r"^[A-Za-z0-9_\-]+$"

VariableType = Literal["workflow", "environment", "node_output", "user_input", "system"]


# --- Node interface -------------------------------------------------------------------


class NodeResult(BaseModel):
    """What a node's execute() returns."""

    success: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

    @classmethod
    def ok(cls, **output: Any) -> Self:
        return cls(success=True, output=output)

    @classmethod
    def fail(cls, error: str, **output: Any) -> Self:
        return cls(success=False, output=output, error=error)


class NodeContext(BaseModel):
    """Everything a node can see while it runs."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    workflow_id: str
    execution_id: str
    # Set by the executor to the id of the node currently running.
    node_id: str | None = None
    # Workflow variables ({key: value}); referenced as {{vars.<key>}}.
    variables: dict[str, Any] = Field(default_factory=dict)
    # {node_id: output} for every node that has already succeeded in this run.
    node_outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Run-time user inputs, keyed by Input node name.
    inputs: dict[str, Any] = Field(default_factory=dict)
    # Provider access. Excluded from serialization; tests inject mocks here.
    services: ExecutionServices = Field(default_factory=ExecutionServices, exclude=True, repr=False)
    # Set by the executor for the running node when an observer wants streamed LLM
    # tokens: await on_token(text, provider). None means "don't stream".
    on_token: Callable[[str, str], Awaitable[None]] | None = Field(default=None, exclude=True, repr=False)
    # Set by the executor: when (time.monotonic()) the running node's timeout expires, so
    # a node that tries several things (an LLM fallback chain) can share out the time.
    deadline: float | None = Field(default=None, exclude=True, repr=False)


# --- Graph ------------------------------------------------------------------------------


class Position(BaseModel):
    x: float = 0.0
    y: float = 0.0


class GraphNode(BaseModel):
    # extra="allow" keeps editor-only React Flow fields (style, selected, ...) round-tripping.
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1, max_length=100, pattern=IDENTIFIER_PATTERN)
    type: str = Field(min_length=1, max_length=100)
    label: str | None = Field(default=None, max_length=255)
    position: Position = Field(default_factory=Position)
    config: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = Field(default=None, max_length=255)
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    # Accepts React Flow's camelCase on input; always serialized as snake_case.
    source_handle: str | None = Field(
        default=None, max_length=100, validation_alias=AliasChoices("source_handle", "sourceHandle")
    )
    target_handle: str | None = Field(
        default=None, max_length=100, validation_alias=AliasChoices("target_handle", "targetHandle")
    )

    @model_validator(mode="after")
    def _default_id(self) -> Self:
        if self.id is None:
            handle = f":{self.source_handle}" if self.source_handle else ""
            self.id = f"{self.source}{handle}->{self.target}"
        return self


class GraphVariable(BaseModel):
    key: str = Field(min_length=1, max_length=255, pattern=IDENTIFIER_PATTERN)
    value: str | None = None
    type: VariableType = "workflow"


class WorkflowGraph(BaseModel):
    model_config = ConfigDict(extra="allow")

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    variables: list[GraphVariable] = Field(default_factory=list)


# --- Validation -------------------------------------------------------------------------


class IssueCode(StrEnum):
    UNKNOWN_NODE_TYPE = "unknown_node_type"
    MISSING_CONFIG = "missing_config"
    INVALID_CONFIG = "invalid_config"
    BROKEN_EDGE = "broken_edge"
    INVALID_BRANCH = "invalid_branch"
    CYCLE = "cycle"
    UNRESOLVABLE_REFERENCE = "unresolvable_reference"
    DUPLICATE_NODE_ID = "duplicate_node_id"
    RESERVED_NODE_ID = "reserved_node_id"
    DUPLICATE_VARIABLE = "duplicate_variable"
    DUPLICATE_NAME = "duplicate_name"
    AUTH_MISSING = "auth_missing"
    # PUBLIC_DEMO: the account used up its daily allowance (raised by the API, not by validation).
    DEMO_LIMIT = "demo_limit"


class ValidationIssue(BaseModel):
    code: IssueCode
    message: str
    node_id: str | None = None
    edge_id: str | None = None
    field: str | None = None


# --- Execution results ------------------------------------------------------------------


class NodeStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    # A stop was requested (ExecutionControl.request_stop) before the graph finished.
    STOPPED = "stopped"
    # Not finished: the next node runs on another queue (ExecutionResult.next_queue). The
    # caller hands the run off and resumes it there with `completed` results.
    HANDOFF = "handoff"


class NodeRunResult(BaseModel):
    node_id: str
    node_type: str
    label: str
    status: NodeStatus
    # The node's config after {{...}} resolution — what it actually ran with.
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error: str | None = None
    skip_reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    # What the privacy guard did before the node ran (counts and types, never values).
    privacy: dict[str, Any] | None = None


class ExecutionResult(BaseModel):
    workflow_id: str
    execution_id: str
    status: RunStatus
    # In execution order; skipped nodes appear where they would have run.
    node_results: list[NodeRunResult]
    final_output: dict[str, Any] | None = None
    error: str | None = None
    # Set with status HANDOFF: the queue the run continues on.
    next_queue: str | None = None
    started_at: datetime
    finished_at: datetime
    duration_ms: int

    def result_for(self, node_id: str) -> NodeRunResult:
        return next(r for r in self.node_results if r.node_id == node_id)
