"""Importing this package registers every model on Base.metadata (used by Alembic)."""

from app.db.base import Base
from app.models.credential import Credential
from app.models.deployment import Deployment
from app.models.enums import (
    DocumentStatus,
    ExecutionStatus,
    ExecutionTrigger,
    IntegrationStatus,
    NodeExecutionStatus,
    TriggerType,
    VariableType,
    WorkflowStatus,
)
from app.models.execution import NodeExecution, WorkflowExecution
from app.models.file import UploadedFile
from app.models.integration import Integration
from app.models.knowledge import KnowledgeBase, KnowledgeChunk, KnowledgeDocument
from app.models.resume import ResumeEmail, ResumeRefinement
from app.models.template import Template
from app.models.trigger import NodeState, TriggerEvent, WorkflowTrigger
from app.models.user import User
from app.models.workflow import Workflow, WorkflowEdge, WorkflowNode, WorkflowVariable

__all__ = [
    "Base",
    "Credential",
    "Deployment",
    "DocumentStatus",
    "ExecutionStatus",
    "ExecutionTrigger",
    "Integration",
    "IntegrationStatus",
    "KnowledgeBase",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "NodeExecution",
    "NodeExecutionStatus",
    "NodeState",
    "ResumeEmail",
    "ResumeRefinement",
    "Template",
    "TriggerEvent",
    "TriggerType",
    "UploadedFile",
    "User",
    "VariableType",
    "Workflow",
    "WorkflowEdge",
    "WorkflowExecution",
    "WorkflowNode",
    "WorkflowStatus",
    "WorkflowTrigger",
    "WorkflowVariable",
]
