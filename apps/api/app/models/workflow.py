import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import VariableType, WorkflowStatus, pg_enum

if TYPE_CHECKING:
    from app.models.execution import WorkflowExecution
    from app.models.trigger import WorkflowTrigger
    from app.models.user import User


class Workflow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflows"

    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[WorkflowStatus] = mapped_column(
        pg_enum(WorkflowStatus, "workflow_status"),
        default=WorkflowStatus.DRAFT,
        server_default=WorkflowStatus.DRAFT.value,
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # Full React Flow graph ({nodes, edges, viewport}) as authored in the editor.
    graph_json: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Trigger safety (schedule, email, webhook runs; manual runs don't count): at most this
    # many triggered runs start per rolling hour, and a trigger switches itself off after
    # this many failed runs in a row (0 = never).
    max_runs_per_hour: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    max_consecutive_failures: Mapped[int] = mapped_column(Integer, default=3, server_default="3")

    owner: Mapped["User"] = relationship(back_populates="workflows")
    nodes: Mapped[list["WorkflowNode"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", passive_deletes=True
    )
    edges: Mapped[list["WorkflowEdge"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", passive_deletes=True
    )
    variables: Mapped[list["WorkflowVariable"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", passive_deletes=True
    )
    executions: Mapped[list["WorkflowExecution"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", passive_deletes=True
    )
    triggers: Mapped[list["WorkflowTrigger"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", passive_deletes=True
    )


class WorkflowNode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflow_nodes"

    __table_args__ = (UniqueConstraint("workflow_id", "node_key"),)

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    # The node's id in graph_json (e.g. "gemini") — what {{gemini.response}} refers to.
    node_key: Mapped[str] = mapped_column(String(100))
    node_type: Mapped[str] = mapped_column(String(100))
    label: Mapped[str] = mapped_column(String(255))
    position_x: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    position_y: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    config_json: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")

    workflow: Mapped["Workflow"] = relationship(back_populates="nodes")


class WorkflowEdge(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "workflow_edges"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    source_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_nodes.id", ondelete="CASCADE"), index=True
    )
    target_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_nodes.id", ondelete="CASCADE"), index=True
    )
    source_handle: Mapped[str | None] = mapped_column(String(100))
    target_handle: Mapped[str | None] = mapped_column(String(100))

    workflow: Mapped["Workflow"] = relationship(back_populates="edges")
    source_node: Mapped["WorkflowNode"] = relationship(foreign_keys=[source_node_id])
    target_node: Mapped["WorkflowNode"] = relationship(foreign_keys=[target_node_id])


class WorkflowVariable(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "workflow_variables"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(255))
    value: Mapped[str | None] = mapped_column(Text)
    var_type: Mapped[VariableType] = mapped_column(
        pg_enum(VariableType, "variable_type"),
        default=VariableType.WORKFLOW,
        server_default=VariableType.WORKFLOW.value,
    )

    workflow: Mapped["Workflow"] = relationship(back_populates="variables")
