import enum

from sqlalchemy import Enum as SAEnum


def pg_enum(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    """Postgres ENUM that stores the member *values* (e.g. "draft"), not the names."""
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


class WorkflowStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class VariableType(str, enum.Enum):
    WORKFLOW = "workflow"
    ENVIRONMENT = "environment"
    NODE_OUTPUT = "node_output"
    USER_INPUT = "user_input"
    SYSTEM = "system"


class ExecutionStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    STOPPED = "stopped"


class ExecutionTrigger(str, enum.Enum):
    MANUAL = "manual"
    # A deployment's endpoint (POST /api/v1/deployments/{id}/run) called with its API key.
    WEBHOOK = "webhook"
    SCHEDULE = "schedule"
    # A new email matched a workflow's email trigger.
    EMAIL = "email"
    EVENT = "event"
    # Legacy: deployment runs before triggers existed (the migration renamed them webhook).
    API = "api"


class TriggerType(str, enum.Enum):
    SCHEDULE = "schedule"
    EMAIL = "email"
    WEBHOOK = "webhook"


class NodeExecutionStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class IntegrationStatus(str, enum.Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"
