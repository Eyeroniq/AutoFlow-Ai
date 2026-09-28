"""Importing this package registers every model on Base.metadata (used by Alembic)."""

from app.db.base import Base
from app.models.credential import Credential
from app.models.enums import (
    ExecutionStatus,
    ExecutionTrigger,
    IntegrationStatus,
    NodeExecutionStatus,
    VariableType,
    WorkflowStatus,
)
from app.models.execution import NodeExecution, WorkflowExecution
from app.models.integration import Integration
from app.models.template import Template
from app.models.user import User
from app.models.workflow import Workflow, WorkflowEdge, WorkflowNode, WorkflowVariable

__all__ = [
    "Base",
    "Credential",
    "ExecutionStatus",
    "ExecutionTrigger",
    "Integration",
    "IntegrationStatus",
    "NodeExecution",
    "NodeExecutionStatus",
    "Template",
    "User",
    "VariableType",
    "Workflow",
    "WorkflowEdge",
    "WorkflowExecution",
    "WorkflowNode",
    "WorkflowStatus",
    "WorkflowVariable",
]
