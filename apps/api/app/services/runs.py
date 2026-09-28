"""Synchronous workflow runs: execute the graph in-request and persist the history.

Phase 3 moves execution to a Celery worker; this module's persistence stays the same.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from flowforge_engine import (
    ExecutionServices,
    NodeContext,
    ValidationIssue,
    WorkflowGraph,
    execute_graph,
    validate_workflow,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.enums import ExecutionStatus, ExecutionTrigger, NodeExecutionStatus
from app.models.execution import NodeExecution, WorkflowExecution
from app.models.user import User
from app.models.workflow import Workflow, WorkflowNode
from app.schemas.execution import ExecutionDetail, NodeExecutionRead

logger = logging.getLogger(__name__)


class InvalidWorkflowGraph(Exception):
    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        super().__init__(f"{len(issues)} validation error(s)")


async def run_workflow(
    db: AsyncSession,
    workflow: Workflow,
    user: User,
    inputs: dict[str, Any],
    services: ExecutionServices,
) -> uuid.UUID:
    """Validate, execute, and persist one run. Returns the WorkflowExecution id.

    Raises InvalidWorkflowGraph (and records nothing) if the graph doesn't validate.
    """
    graph = WorkflowGraph.model_validate(workflow.graph_json)
    issues = validate_workflow(graph)
    if issues:
        raise InvalidWorkflowGraph(issues)

    execution = WorkflowExecution(
        id=uuid.uuid4(),
        workflow_id=workflow.id,
        status=ExecutionStatus.RUNNING,
        trigger=ExecutionTrigger.MANUAL,
        started_at=datetime.now(UTC),
        triggered_by_user_id=user.id,
    )
    db.add(execution)
    # Commit the "running" row first so the run is visible even if persisting results fails.
    await db.commit()

    context = NodeContext(
        workflow_id=str(workflow.id),
        execution_id=str(execution.id),
        inputs=inputs,
        services=services,
    )
    try:
        result = await execute_graph(graph, context, node_timeout=settings.WORKFLOW_NODE_TIMEOUT_SECONDS)
    except Exception as exc:  # engine bug: record it rather than leaving the run "running"
        logger.exception("workflow run crashed", extra={"execution_id": str(execution.id)})
        execution.status = ExecutionStatus.FAILED
        execution.finished_at = datetime.now(UTC)
        execution.error_message = f"Engine error: {type(exc).__name__}: {exc}"
        await db.commit()
        return execution.id

    node_rows = {
        row.node_key: row.id
        for row in await db.scalars(select(WorkflowNode).where(WorkflowNode.workflow_id == workflow.id))
    }
    for node_result in result.node_results:
        # JSON-mode dump so whatever a node returned is serializable into JSONB.
        payload = node_result.model_dump(mode="json", include={"input", "output"})
        db.add(NodeExecution(
            execution_id=execution.id,
            node_id=node_rows.get(node_result.node_id),
            node_key=node_result.node_id,
            node_type=node_result.node_type,
            node_label=node_result.label,
            status=NodeExecutionStatus(node_result.status.value),
            input_json=payload["input"],
            output_json=payload["output"],
            error_message=node_result.error or node_result.skip_reason,
            started_at=node_result.started_at,
            finished_at=node_result.finished_at,
            duration_ms=node_result.duration_ms,
        ))

    execution.status = ExecutionStatus.SUCCESS if result.status == "success" else ExecutionStatus.FAILED
    execution.finished_at = result.finished_at
    execution.final_output_json = result.model_dump(mode="json", include={"final_output"})["final_output"]
    execution.error_message = result.error
    await db.commit()

    logger.info(
        "workflow run finished",
        extra={
            "workflow_id": str(workflow.id),
            "execution_id": str(execution.id),
            "status": execution.status.value,
            "duration_ms": result.duration_ms,
        },
    )
    return execution.id


def _node_order(row: NodeExecutionRead) -> tuple[bool, datetime, str]:
    # Run order; skipped nodes (never started) last.
    return (row.started_at is None, row.started_at or datetime.max.replace(tzinfo=UTC), row.node_key)


async def load_execution_detail(
    db: AsyncSession, execution_id: uuid.UUID, user: User
) -> ExecutionDetail | None:
    """An execution with its node rows, only if it belongs to one of `user`'s workflows."""
    execution = await db.scalar(
        select(WorkflowExecution)
        .join(Workflow, Workflow.id == WorkflowExecution.workflow_id)
        .where(WorkflowExecution.id == execution_id, Workflow.owner_id == user.id)
        .options(selectinload(WorkflowExecution.node_executions))
        .execution_options(populate_existing=True)
    )
    if execution is None:
        return None
    detail = ExecutionDetail.model_validate(execution)
    detail.node_executions.sort(key=_node_order)
    return detail
