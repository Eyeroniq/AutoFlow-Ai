"""Stopping executions and recovering ones whose worker died."""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.enums import ExecutionStatus, NodeExecutionStatus
from app.models.execution import WorkflowExecution
from app.services.events import EventPublisher, iso, set_stop_flag
from app.services.runs import TERMINAL_STATUSES, close_out_nodes, finish_execution, utcnow
from app.services.task_queue import TaskQueue

logger = logging.getLogger(__name__)

STOPPED_BY_USER = "Stopped by user"


class ExecutionAlreadyFinished(Exception):
    def __init__(self, status: ExecutionStatus):
        self.status = status
        super().__init__(f"Execution already finished with status '{status.value}'")


@dataclass
class StopOutcome:
    # True once the execution is in a terminal state; False means the worker has been
    # told to stop but hasn't confirmed within the wait (e.g. an email is mid-send).
    finished: bool
    how: str


async def _status(db: AsyncSession, execution_id: uuid.UUID) -> tuple[ExecutionStatus, datetime | None, str | None]:
    row = (await db.execute(
        select(WorkflowExecution.status, WorkflowExecution.heartbeat_at, WorkflowExecution.celery_task_id)
        .where(WorkflowExecution.id == execution_id)
    )).one()
    return row.status, row.heartbeat_at, row.celery_task_id


async def stop_execution(
    db: AsyncSession,
    execution_id: uuid.UUID,
    *,
    redis: Redis,
    task_queue: TaskQueue,
    wait_seconds: float | None = None,
) -> StopOutcome:
    """Stop a pending or running execution (the caller has checked ownership).

    - pending: marked stopped right here and its Celery task revoked; if the task is
      delivered anyway, its claim fails and it does nothing.
    - running: a stop flag is set in Redis; the worker cancels the current node if it is
      interruptible (otherwise lets it finish), skips the rest, and marks the run stopped.
      We wait up to `wait_seconds` for that. If the worker is unresponsive (stale
      heartbeat) the execution is marked stopped here instead.
    """
    publisher = EventPublisher(redis)
    wait_seconds = settings.EXECUTION_STOP_WAIT_SECONDS if wait_seconds is None else wait_seconds
    status, heartbeat_at, task_id = await _status(db, execution_id)
    if status in TERMINAL_STATUSES:
        raise ExecutionAlreadyFinished(status)

    now = utcnow()
    await db.execute(
        update(WorkflowExecution).where(WorkflowExecution.id == execution_id)
        .values(stop_requested_at=now).execution_options(synchronize_session=False)
    )
    await db.commit()

    if status is ExecutionStatus.PENDING and await finish_execution(
        db, None, execution_id, ExecutionStatus.STOPPED,
        error=f"{STOPPED_BY_USER} before a worker started it", from_statuses=(ExecutionStatus.PENDING,),
    ):
        await close_out_nodes(
            db, publisher, execution_id,
            pending_reason="Not run: stopped by user before the execution started", running_reason=STOPPED_BY_USER,
        )
        await publish_finished(db, publisher, execution_id)
        if task_id:
            try:
                await task_queue.revoke(task_id)
            except Exception as exc:  # the claim guard makes a late delivery harmless anyway
                logger.warning("could not revoke task", extra={"execution_id": str(execution_id), "error": str(exc)})
        return StopOutcome(finished=True, how="stopped before it started")

    # Running (or it just got claimed): ask the worker to stop.
    try:
        await set_stop_flag(redis, execution_id)
    except RedisError as exc:
        logger.warning("could not set the stop flag", extra={"execution_id": str(execution_id), "error": str(exc)})

    status, heartbeat_at, _ = await _status(db, execution_id)
    stale = heartbeat_at is None or heartbeat_at < now - timedelta(seconds=settings.EXECUTION_STALE_AFTER_SECONDS)
    if status is ExecutionStatus.RUNNING and stale and await finalize_dead(
        db, publisher, execution_id, ExecutionStatus.STOPPED, f"{STOPPED_BY_USER} (its worker was unresponsive)",
        running_status=NodeExecutionStatus.SKIPPED, running_reason=f"Interrupted: {STOPPED_BY_USER}",
        pending_reason=f"Not run: {STOPPED_BY_USER}",
    ):
        return StopOutcome(finished=True, how="stopped here: the worker was unresponsive")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    while True:
        status, _, _ = await _status(db, execution_id)
        if status in TERMINAL_STATUSES:
            return StopOutcome(finished=True, how="stopped by the worker")
        if loop.time() >= deadline:
            return StopOutcome(finished=False, how="stop requested; waiting for the worker")
        await asyncio.sleep(0.1)


async def publish_finished(db: AsyncSession, publisher: EventPublisher, execution_id: uuid.UUID) -> None:
    """The execution.finished event for a row finished without a publisher (so node events go first)."""
    row = await db.get(WorkflowExecution, execution_id, populate_existing=True)
    assert row is not None
    duration_ms = None
    if row.started_at and row.finished_at:
        duration_ms = round((row.finished_at - row.started_at).total_seconds() * 1000)
    await publisher.publish(
        execution_id, "execution.finished",
        status=row.status.value, final_output=row.final_output_json, error=row.error_message,
        started_at=iso(row.started_at), finished_at=iso(row.finished_at), duration_ms=duration_ms,
    )


async def finalize_dead(
    db: AsyncSession,
    publisher: EventPublisher,
    execution_id: uuid.UUID,
    status: ExecutionStatus,
    error: str,
    *,
    running_status: NodeExecutionStatus,
    running_reason: str,
    pending_reason: str,
) -> bool:
    """Finish a running execution whose worker is gone. False if it finished meanwhile."""
    if not await finish_execution(db, None, execution_id, status, error=error):
        return False
    await close_out_nodes(
        db, publisher, execution_id,
        pending_reason=pending_reason, running_reason=running_reason, running_status=running_status,
    )
    await publish_finished(db, publisher, execution_id)
    logger.warning("finalized an execution whose worker is gone",
                   extra={"execution_id": str(execution_id), "status": status.value, "error": error})
    return True


async def recover_stale_executions(
    db: AsyncSession,
    redis: Redis,
    *,
    restarted_worker: str | None = None,
    restarted_before: datetime | None = None,
) -> list[uuid.UUID]:
    """Mark executions that can no longer finish as failed. Safe to run concurrently.

    - running on `restarted_worker` since before it booted (`restarted_before`): that
      worker process was restarted, so the run died with it -- recovered immediately;
    - running with no heartbeat for EXECUTION_STALE_AFTER_SECONDS: its worker crashed;
    - pending for longer than EXECUTION_PENDING_TIMEOUT_SECONDS: no worker picked it up.
    """
    publisher = EventPublisher(redis)
    now = utcnow()
    stale_cutoff = now - timedelta(seconds=settings.EXECUTION_STALE_AFTER_SECONDS)
    pending_cutoff = now - timedelta(seconds=settings.EXECUTION_PENDING_TIMEOUT_SECONDS)
    candidates: list[tuple[uuid.UUID, str]] = []

    if restarted_worker:
        rows = await db.execute(
            select(WorkflowExecution.id).where(
                WorkflowExecution.status == ExecutionStatus.RUNNING,
                WorkflowExecution.worker_hostname == restarted_worker,
                WorkflowExecution.heartbeat_at < (restarted_before or now),
            )
        )
        for (execution_id,) in rows:
            candidates.append((execution_id, (
                f"The worker '{restarted_worker}' restarted while this execution was running "
                "(crash, kill, or redeploy), so the run was interrupted"
            )))

    rows = await db.execute(
        select(WorkflowExecution.id, WorkflowExecution.worker_hostname, WorkflowExecution.heartbeat_at).where(
            WorkflowExecution.status == ExecutionStatus.RUNNING,
            WorkflowExecution.heartbeat_at < stale_cutoff,
        )
    )
    seen = {c[0] for c in candidates}
    for execution_id, worker, heartbeat_at in rows:
        if execution_id not in seen:
            silent = round((now - heartbeat_at).total_seconds())
            candidates.append((execution_id, (
                f"No heartbeat from worker '{worker}' for {silent}s: it crashed or was killed "
                "while running this execution"
            )))

    recovered = []
    for execution_id, error in candidates:
        if await finalize_dead(
            db, publisher, execution_id, ExecutionStatus.FAILED, error,
            running_status=NodeExecutionStatus.FAILED,
            running_reason="Interrupted: the worker stopped while this node was running",
            pending_reason="Not run: the worker stopped before reaching this node",
        ):
            recovered.append(execution_id)

    pending = await db.execute(
        select(WorkflowExecution.id).where(
            WorkflowExecution.status == ExecutionStatus.PENDING, WorkflowExecution.created_at < pending_cutoff
        )
    )
    for (execution_id,) in pending:
        error = (
            f"No worker picked this execution up within {settings.EXECUTION_PENDING_TIMEOUT_SECONDS:g}s "
            "(is a worker running for its queue?)"
        )
        if await finish_execution(db, None, execution_id, ExecutionStatus.FAILED, error=error,
                                  from_statuses=(ExecutionStatus.PENDING,)):
            await close_out_nodes(db, publisher, execution_id, pending_reason="Not run: never picked up",
                                  running_reason="Not run: never picked up")
            await publish_finished(db, publisher, execution_id)
            recovered.append(execution_id)

    if recovered:
        logger.warning("recovered stale executions", extra={"execution_ids": [str(i) for i in recovered]})
    return recovered
