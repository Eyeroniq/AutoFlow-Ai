import uuid
from datetime import datetime
from typing import Annotated, Any

from flowforge_engine import ValidationIssue, WorkflowGraph
from flowforge_engine.testing import example_graph
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models.enums import WorkflowStatus


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


class WorkflowRead(WorkflowSummary):
    graph: WorkflowGraph = Field(validation_alias="graph_json")


class WorkflowValidation(BaseModel):
    valid: bool
    errors: list[ValidationIssue]
