import re
import uuid
from datetime import datetime
from typing import Annotated, Any

from flowforge_engine import ValidationIssue, WorkflowGraph
from flowforge_engine.testing import example_graph
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.models.enums import ExecutionStatus, WorkflowStatus
from app.schemas.trigger import TriggerBrief


def _example_graph() -> dict[str, Any]:
    graph = example_graph().model_dump(mode="json")
    for index, node in enumerate(graph["nodes"]):
        node["position"] = {"x": index * 260, "y": 0}
    return graph


EXAMPLE_GRAPH = _example_graph()

# Stripped before the length check, so "   " is rejected.
WorkflowName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"name": "Summarize and email", "description": "Phase 2 demo"}]}
    )

    name: WorkflowName
    description: str | None = Field(default=None, max_length=5000)


class WorkflowUpdate(BaseModel):
    """Only the fields you send are changed. `graph` replaces all nodes, edges, and variables."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"graph": EXAMPLE_GRAPH}]})

    name: WorkflowName | None = None
    description: str | None = Field(default=None, max_length=5000)
    status: WorkflowStatus | None = None
    graph: WorkflowGraph | None = None


class WorkflowSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    status: WorkflowStatus
    version: int
    created_at: datetime
    updated_at: datetime


class LastExecution(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: ExecutionStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class WorkflowListItem(WorkflowSummary):
    """A workflow in the dashboard list: plus its size and most recent run."""

    node_count: int
    last_execution: LastExecution | None
    triggers: list[TriggerBrief] = Field(default_factory=list, description="Saved triggers: type, on/off, auto-disabled.")


class WorkflowRead(WorkflowSummary):
    graph: WorkflowGraph = Field(validation_alias="graph_json")


class WorkflowValidation(BaseModel):
    valid: bool
    errors: list[ValidationIssue]


class ValidateRequest(BaseModel):
    """Optional body for /validate: check this graph instead of the saved one (the editor
    validates unsaved edits with it; nothing is stored)."""

    graph: WorkflowGraph | None = None


class NodeTestRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{
                "upstream_outputs": {"input": {"value": "tides", "topic": "tides"}},
                "variables": {"recipient": "you@example.com"},
            }]
        }
    )

    config: dict[str, Any] | None = Field(
        default=None, description="Test this config instead of the saved one (e.g. unsaved edits)."
    )
    upstream_outputs: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Sample outputs of upstream nodes, keyed by node id, for {{node.key}} references.",
    )
    variables: dict[str, Any] = Field(
        default_factory=dict, description="Workflow variables; merged over the saved graph's variables."
    )
    inputs: dict[str, Any] = Field(default_factory=dict, description="Run inputs, for Input nodes.")


class NodeTestResult(BaseModel):
    node_key: str
    node_type: str
    label: str
    status: str = Field(description="success, failed, or skipped")
    input: dict[str, Any] | None = Field(description="The config after {{...}} resolution.")
    output: dict[str, Any] | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None


class PrivacySettings(BaseModel):
    """A workflow's privacy settings (GET/PUT /api/workflows/{id}/privacy)."""

    mask_stored_io: bool = Field(
        default=True,
        description="Store step inputs/outputs, live events, and the final output with secrets and IDs masked "
        "([REDACTED:<TYPE>]). The real values still reach the next steps.",
    )
    detect_personal_data: bool = Field(
        default=False,
        description="Also find names, emails, phone numbers, and locations (guard, masking, and the report).",
    )
    allowlist: list[str] = Field(
        default_factory=list, max_length=200,
        description="Known-safe values the detectors should ignore: exact text (any case), or re:<regex>.",
    )

    @field_validator("allowlist")
    @classmethod
    def _entries(cls, entries: list[str]) -> list[str]:
        cleaned = []
        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue
            if len(entry) > 200:
                raise ValueError("allowlist entries are at most 200 characters")
            if entry.startswith("re:"):
                try:
                    re.compile(entry[3:])
                except re.error as exc:
                    raise ValueError(f"invalid regex in '{entry}': {exc}") from None
            cleaned.append(entry)
        return cleaned
