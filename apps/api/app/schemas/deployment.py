import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ExecutionStatus


class DeploymentCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"workflow_id": "00000000-0000-0000-0000-000000000000"}]})

    workflow_id: uuid.UUID
    description: str | None = Field(
        default=None, max_length=1000,
        description="What the pipeline does, in a sentence or two (the Telegram Command Center matches messages "
        "against it). Required on the first deploy; a redeploy without one keeps the current description.",
    )


class DeploymentInput(BaseModel):
    """One Input node of the deployed graph: a key the caller may send in `inputs`."""

    node_id: str
    label: str
    name: str = Field(description="The key to send in `inputs`.")
    type: str = Field(description="text, number, json, or file (an uploaded file's id).")
    required: bool = Field(description="True when the caller must send it (no default).")
    default: Any = None


class DeploymentOutput(BaseModel):
    """One Output node: a key of the run's `final_output`."""

    node_id: str
    label: str
    name: str


class DeploymentRead(BaseModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    name: str = Field(description="The pipeline's name when it was (re)deployed.")
    version: int = Field(description="1 on the first deploy, +1 per redeploy.")
    workflow_version: int = Field(description="The workflow's version that was deployed.")
    endpoint: str = Field(description="POST here (relative to the API's base URL) to run it.")
    api_key_prefix: str = Field(description="The API key's first characters, to recognize it.")
    description: str = Field(default="", description="What it does (matched against Telegram messages).")
    side_effects: bool = Field(
        default=False, description="It sends or writes somewhere (email, messages, non-GET HTTP, Notion/Airtable): "
        "runs from Telegram wait for a Confirm tap.",
    )
    inputs: list[DeploymentInput]
    outputs: list[DeploymentOutput]
    key_created_at: datetime
    deployed_at: datetime
    revoked_at: datetime | None = Field(
        default=None, description="When it was undeployed (the endpoint answers 404); null while it's live."
    )
    created_at: datetime
    updated_at: datetime


class DeploymentWithKey(DeploymentRead):
    api_key: str | None = Field(
        description="The API key, only in the response that issued it (the first deploy, or a key "
        "rotation). It is stored hashed and can't be shown again; null when the existing key was kept."
    )


class DeploymentRunRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"inputs": {"topic": "the history of workflow automation"}}]})

    inputs: dict[str, Any] = Field(
        default_factory=dict, description="Values for the Input nodes, keyed by each one's `name`."
    )


class DeploymentExecutionLinks(BaseModel):
    status: str = Field(description="GET with the same API key: the run's status and final output.")


class DeploymentExecution(BaseModel):
    """A run started through a deployment, as its caller sees it: status and final output,
    no per-node internals."""

    execution_id: uuid.UUID
    deployment_id: uuid.UUID
    status: ExecutionStatus
    final_output: dict[str, Any] | None = Field(description="Keyed by Output node name, once the run succeeded.")
    error: str | None = Field(description="Why the run failed or was stopped.")
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    links: DeploymentExecutionLinks
