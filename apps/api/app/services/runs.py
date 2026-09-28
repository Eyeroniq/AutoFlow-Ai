"""Workflow runs: create a pending execution, then run it (in a Celery worker, or in-request
for `?sync=true`) while recording every node's state and publishing live events.

State machine (workflow_executions.status):

    pending --claim--> running --> success | failed | stopped
       |                  \\--(worker dies, heartbeat goes stale)--> failed   (app.services.control)
       \\--(stopped before a worker claims it / never picked up)--> stopped | failed

Claiming is a compare-and-set (UPDATE ... WHERE status = 'pending'), so a run executes at
most once however many times its task is delivered.
"""

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from flowforge_engine import (
    ExecutionControl,
    ExecutionHooks,
    ExecutionServices,
    GraphNode,
    GraphValidationFailed,
    NodeContext,
    NodeRunResult,
    RunStatus,
    ValidationIssue,
    WorkflowGraph,
    execute_graph,
    get_node_definition,
    topological_sort,
    validate_workflow,
)
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.session import SessionFactory
from app.models.enums import ExecutionStatus, ExecutionTrigger, NodeExecutionStatus
from app.models.execution import NodeExecution, WorkflowExecution
from app.models.user import User
from app.models.workflow import Workflow, WorkflowNode
from app.schemas.execution import ExecutionDetail, NodeExecutionRead
from app.services.credentials import build_execution_services
from app.services.events import EventPublisher, iso, stop_flag_set

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({ExecutionStatus.SUCCESS, ExecutionStatus.FAILED, ExecutionStatus.STOPPED})
_RUN_STATUS = {
    RunStatus.SUCCESS: ExecutionStatus.SUCCESS,
    RunStatus.FAILED: ExecutionStatus.FAILED,
    RunStatus.STOPPED: ExecutionStatus.STOPPED,
}
_NODE_EVENT = {
    NodeExecutionStatus.SUCCESS: "node.succeeded",
    NodeExecutionStatus.FAILED: "node.failed",
    NodeExecutionStatus.SKIPPED: "node.skipped",
}
# Errors that mean "the infrastructure is unavailable", as opposed to a workflow problem.
INFRASTRUCTURE_ERRORS: tuple[type[BaseException], ...] = (OperationalError, InterfaceError, DBAPIError, OSError, RedisError)


class InvalidWorkflowGraph(Exception):
    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        super().__init__(f"{len(issues)} validation error(s)")


class InfrastructureUnavailable(Exception):
    """The database/broker couldn't be reached before the run started; safe to retry."""


def utcnow() -> datetime:
    return datetime.now(UTC)


# --- creating ---------------------------------------------------------------------------


async def create_execution(
    db: AsyncSession,
    workflow: Workflow,
    user: User,
    inputs: dict[str, Any],
    services: ExecutionServices,
    *,
    queue: str | None = None,
) -> WorkflowExecution:
    """Validate the saved graph and record a pending execution with one pending row per node.

    Raises InvalidWorkflowGraph (and records nothing) if the graph doesn't validate,
    including "Authentication missing" for providers without credentials.
    """
    graph = WorkflowGraph.model_validate(workflow.graph_json)
    issues = validate_workflow(graph, services=services)
    if issues:
        raise InvalidWorkflowGraph(issues)

    execution_id = uuid.uuid4()
    execution = WorkflowExecution(
        id=execution_id,
        workflow_id=workflow.id,
        status=ExecutionStatus.PENDING,
        trigger=ExecutionTrigger.MANUAL,
        triggered_by_user_id=user.id,
        inputs_json=inputs,
        graph_json=graph.model_dump(mode="json"),
        queue=queue,
        # Queued runs use the execution id as their Celery task id (see task_queue).
        celery_task_id=str(execution_id) if queue else None,
    )
    db.add(execution)
    row_ids = {
        row.node_key: row.id
        for row in await db.scalars(select(WorkflowNode).where(WorkflowNode.workflow_id == workflow.id))
    }
    by_id = {node.id: node for node in graph.nodes}
    for position, node_id in enumerate(topological_sort(graph.nodes, graph.edges)):
        node = by_id[node_id]
        definition = get_node_definition(node.type)
        db.add(NodeExecution(
            execution_id=execution.id,
            node_id=row_ids.get(node.id),
            node_key=node.id,
            node_type=node.type,
            node_label=node.label or (definition.label if definition else node.type),
            position=position,
            status=NodeExecutionStatus.PENDING,
        ))
    await db.commit()
    return execution


# --- shared state transitions (also used by stop and crash recovery) ---------------------


async def close_out_nodes(
    db: AsyncSession,
    publisher: EventPublisher | None,
    execution_id: uuid.UUID,
    *,
    pending_reason: str,
    running_reason: str,
    running_status: NodeExecutionStatus = NodeExecutionStatus.SKIPPED,
) -> None:
    """Finish every node row that is still pending/running (commits, then publishes)."""
    now = utcnow()
    rows = list(await db.scalars(
        select(NodeExecution)
        .where(
            NodeExecution.execution_id == execution_id,
            NodeExecution.status.in_([NodeExecutionStatus.PENDING, NodeExecutionStatus.RUNNING]),
        )
        .order_by(NodeExecution.position, NodeExecution.node_key)  # events in execution order
        .execution_options(populate_existing=True)
    ))
    events = []
    for row in rows:
        if row.status is NodeExecutionStatus.RUNNING:
            row.status, row.error_message = running_status, running_reason
            row.finished_at = now
            if row.started_at:
                row.duration_ms = round((now - row.started_at).total_seconds() * 1000)
        else:
            row.status, row.error_message = NodeExecutionStatus.SKIPPED, pending_reason
        events.append(row)
    await db.commit()
    if publisher is not None:
        for row in events:
            await publisher.publish(execution_id, _NODE_EVENT[row.status], **_node_event_fields(row))


def _node_event_fields(row: NodeExecution) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "node_key": row.node_key,
        "node_type": row.node_type,
        "label": row.node_label,
        "status": row.status.value,
        "started_at": iso(row.started_at),
        "finished_at": iso(row.finished_at),
        "duration_ms": row.duration_ms,
    }
    if row.status is NodeExecutionStatus.FAILED:
        fields["error"] = row.error_message
    elif row.status is NodeExecutionStatus.SKIPPED:
        fields["reason"] = row.error_message
    return fields


async def finish_execution(
    db: AsyncSession,
    publisher: EventPublisher | None,
    execution_id: uuid.UUID,
    status: ExecutionStatus,
    *,
    error: str | None,
    final_output: dict[str, Any] | None = None,
    from_statuses: tuple[ExecutionStatus, ...] = (ExecutionStatus.RUNNING,),
) -> bool:
    """Move the execution to a terminal status if it is still in `from_statuses`.

    Returns False (changing nothing) when someone else already finished it, e.g. the
    run was stopped or recovered while this code was working.
    """
    now = utcnow()
    result = await db.execute(
        update(WorkflowExecution)
        .where(WorkflowExecution.id == execution_id, WorkflowExecution.status.in_(from_statuses))
        .values(status=status, finished_at=now, error_message=error, final_output_json=final_output)
        .returning(WorkflowExecution.id, WorkflowExecution.started_at)
        .execution_options(synchronize_session=False)
    )
    row = result.first()
    await db.commit()
    if row is None:
        return False
    started_at = row.started_at
    if publisher is not None:
        await publisher.publish(
            execution_id,
            "execution.finished",
            status=status.value,
            final_output=final_output,
            error=error,
            started_at=iso(started_at),
            finished_at=iso(now),
            duration_ms=round((now - started_at).total_seconds() * 1000) if started_at else None,
        )
    return True


# --- running ----------------------------------------------------------------------------


class ExecutionRecorder(ExecutionHooks):
    """Writes each node transition to its row, then publishes the matching event."""

    def __init__(self, db: AsyncSession, publisher: EventPublisher, execution_id: uuid.UUID):
        self.db, self.publisher, self.execution_id = db, publisher, execution_id

    def _row(self, node_key: str) -> Any:
        return update(NodeExecution).where(
            NodeExecution.execution_id == self.execution_id, NodeExecution.node_key == node_key
        ).execution_options(synchronize_session=False)

    async def node_started(self, node: GraphNode, label: str, started_at: datetime) -> None:
        await self.db.execute(self._row(node.id).values(status=NodeExecutionStatus.RUNNING, started_at=started_at))
        await self.db.commit()
        await self.publisher.publish(
            self.execution_id, "node.started",
            node_key=node.id, node_type=node.type, label=label, status="running", started_at=iso(started_at),
        )

    async def node_finished(self, result: NodeRunResult) -> None:
        # JSON-mode dump so whatever a node returned is serializable into JSONB.
        payload = result.model_dump(mode="json", include={"input", "output"})
        status = NodeExecutionStatus(result.status.value)
        await self.db.execute(self._row(result.node_id).values(
            status=status,
            input_json=payload["input"],
            output_json=payload["output"],
            error_message=result.error or result.skip_reason,
            started_at=result.started_at,
            finished_at=result.finished_at,
            duration_ms=result.duration_ms,
        ))
        await self.db.commit()
        fields: dict[str, Any] = {
            "node_key": result.node_id,
            "node_type": result.node_type,
            "label": result.label,
            "status": status.value,
            "started_at": iso(result.started_at),
            "finished_at": iso(result.finished_at),
            "duration_ms": result.duration_ms,
        }
        if status is NodeExecutionStatus.SUCCESS:
            fields |= {"input": payload["input"], "output": payload["output"]}
        elif status is NodeExecutionStatus.FAILED:
            fields |= {"input": payload["input"], "error": result.error, "output": payload["output"]}
        else:
            fields["reason"] = result.skip_reason
        await self.publisher.publish(self.execution_id, _NODE_EVENT[status], **fields)

    async def node_token(self, node_id: str, text: str, provider: str) -> None:
        await self.publisher.publish(self.execution_id, "node.token", node_key=node_id, text=text, provider=provider)


async def claim_execution(db: AsyncSession, execution_id: uuid.UUID, worker_id: str) -> bool:
    """pending -> running, atomically. False if it isn't pending (already ran, running, stopped)."""
    now = utcnow()
    claimed = await db.scalar(
        update(WorkflowExecution)
        .where(WorkflowExecution.id == execution_id, WorkflowExecution.status == ExecutionStatus.PENDING)
        .values(status=ExecutionStatus.RUNNING, started_at=now, heartbeat_at=now, worker_hostname=worker_id)
        .returning(WorkflowExecution.id)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return claimed is not None


async def _heartbeat(session_factory: SessionFactory, execution_id: uuid.UUID, worker_id: str) -> bool:
    """Refresh heartbeat_at; False once the execution is no longer ours and running."""
    async with session_factory() as db:
        alive = await db.scalar(
            update(WorkflowExecution)
            .where(
                WorkflowExecution.id == execution_id,
                WorkflowExecution.status == ExecutionStatus.RUNNING,
                WorkflowExecution.worker_hostname == worker_id,
            )
            .values(heartbeat_at=utcnow())
            .returning(WorkflowExecution.id)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    return alive is not None


async def _watch(
    execution_id: uuid.UUID,
    control: ExecutionControl,
    *,
    session_factory: SessionFactory,
    redis: Redis,
    worker_id: str,
) -> None:
    """Background loop for a running execution: stop-flag polling and heartbeats."""
    loop = asyncio.get_running_loop()
    last_beat = loop.time()
    redis_warned = False
    while True:
        await asyncio.sleep(settings.EXECUTION_STOP_POLL_SECONDS)
        if not control.stop_requested:
            try:
                if await stop_flag_set(redis, execution_id):
                    logger.info("stop requested", extra={"execution_id": str(execution_id)})
                    control.request_stop("Stopped by user")
                redis_warned = False
            except RedisError as exc:
                if not redis_warned:
                    logger.warning("can't check the stop flag", extra={"execution_id": str(execution_id), "error": str(exc)})
                    redis_warned = True
        if loop.time() - last_beat >= settings.EXECUTION_HEARTBEAT_SECONDS:
            last_beat = loop.time()
            try:
                if not await _heartbeat(session_factory, execution_id, worker_id):
                    control.request_stop(
                        "The execution was finished elsewhere (stopped, or marked failed by crash recovery)",
                        status=RunStatus.FAILED,
                    )
            except INFRASTRUCTURE_ERRORS as exc:
                logger.warning("heartbeat failed", extra={"execution_id": str(execution_id), "error": str(exc)})


async def run_execution(
    execution_id: uuid.UUID,
    *,
    session_factory: SessionFactory,
    redis: Redis,
    worker_id: str,
    services: ExecutionServices | None = None,
) -> dict[str, Any]:
    """Claim and run one execution to completion. Returns a small summary.

    Raises InfrastructureUnavailable only if the database is unreachable *before* the run
    was claimed (the caller may retry). After the claim, every failure is recorded on the
    execution instead, and nothing is retried, so side-effecting nodes never run twice.
    """
    publisher = EventPublisher(redis)
    try:
        async with session_factory() as db:
            if not await claim_execution(db, execution_id, worker_id):
                status = await db.scalar(select(WorkflowExecution.status).where(WorkflowExecution.id == execution_id))
                logger.info(
                    "execution isn't pending; not running it (duplicate delivery or already stopped)",
                    extra={"execution_id": str(execution_id), "status": getattr(status, "value", status)},
                )
                return {"execution_id": str(execution_id), "ran": False, "status": getattr(status, "value", None)}
    except INFRASTRUCTURE_ERRORS as exc:
        raise InfrastructureUnavailable(f"{type(exc).__name__}: {exc}") from exc

    async with session_factory() as db:
        execution = await db.scalar(
            select(WorkflowExecution)
            .where(WorkflowExecution.id == execution_id)
            .options(selectinload(WorkflowExecution.workflow))
            .execution_options(populate_existing=True)
        )
        assert execution is not None
        owner = await db.get(User, execution.workflow.owner_id)
        assert owner is not None
        graph = WorkflowGraph.model_validate(execution.graph_json or execution.workflow.graph_json)
        await publisher.publish(
            execution_id, "execution.started",
            status="running", workflow_id=str(execution.workflow_id), worker=worker_id, queue=execution.queue,
            started_at=iso(execution.started_at),
            nodes=[{"node_key": n.id, "node_type": n.type} for n in graph.nodes],
        )
        control = ExecutionControl()
        loop = asyncio.get_running_loop()
        limit = settings.EXECUTION_TIME_LIMIT_SECONDS
        limit_handle = loop.call_later(
            limit, lambda: control.request_stop(f"Execution exceeded the time limit of {limit:g}s", status=RunStatus.FAILED)
        )
        watcher = asyncio.create_task(
            _watch(execution_id, control, session_factory=session_factory, redis=redis, worker_id=worker_id)
        )
        status, error = ExecutionStatus.FAILED, None
        try:
            if services is None:
                services = await build_execution_services(db, owner)
            context = NodeContext(
                workflow_id=str(execution.workflow_id),
                execution_id=str(execution_id),
                inputs=execution.inputs_json or {},
                services=services,
            )
            result = await execute_graph(
                graph, context,
                node_timeout=settings.WORKFLOW_NODE_TIMEOUT_SECONDS,
                hooks=ExecutionRecorder(db, publisher, execution_id),
                control=control,
            )
            status, error = _RUN_STATUS[result.status], result.error
            final_output = result.model_dump(mode="json", include={"final_output"})["final_output"]
            await finish_execution(db, publisher, execution_id, status, error=error, final_output=final_output)
        except GraphValidationFailed as exc:
            # E.g. a credential was removed between queueing and running.
            error = str(exc)
            await _fail(db, publisher, execution_id, error, "Not run: the workflow failed validation when the run started")
        except Exception as exc:  # engine/recording bug or database trouble mid-run
            logger.exception("workflow run crashed", extra={"execution_id": str(execution_id)})
            error = f"Engine error: {type(exc).__name__}: {exc}"
            with contextlib.suppress(Exception):
                await db.rollback()
                await _fail(db, publisher, execution_id, error, "Not run: the execution crashed")
        finally:
            limit_handle.cancel()
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    logger.info(
        "workflow run finished",
        extra={"execution_id": str(execution_id), "status": status.value, "worker": worker_id},
    )
    return {"execution_id": str(execution_id), "ran": True, "status": status.value, "error": error}


async def _fail(db: AsyncSession, publisher: EventPublisher, execution_id: uuid.UUID, error: str, reason: str) -> None:
    await close_out_nodes(
        db, publisher, execution_id,
        pending_reason=reason, running_reason=f"Interrupted: {error}", running_status=NodeExecutionStatus.FAILED,
    )
    await finish_execution(db, publisher, execution_id, ExecutionStatus.FAILED, error=error)


# --- reading ----------------------------------------------------------------------------


def _node_order(row: NodeExecutionRead) -> tuple[bool, datetime, int, str]:
    # Run order; nodes that never started (pending, skipped) after, in execution order.
    started = row.started_at or datetime.max.replace(tzinfo=UTC)
    return (row.started_at is None, started, row.position if row.position is not None else 1 << 30, row.node_key)


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


async def owned_execution(db: AsyncSession, execution_id: uuid.UUID, user: User) -> WorkflowExecution | None:
    return await db.scalar(
        select(WorkflowExecution)
        .join(Workflow, Workflow.id == WorkflowExecution.workflow_id)
        .where(WorkflowExecution.id == execution_id, Workflow.owner_id == user.id)
        .execution_options(populate_existing=True)
    )
