"""Worker-side task and lifecycle hooks.

Each task runs one execution inside `asyncio.run`, with its own database engine and
Redis client (a prefork child has no event loop to share).
"""

import asyncio
import contextlib
import logging
import socket
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from celery import signals
from celery.exceptions import SoftTimeLimitExceeded
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.redis import new_redis
from app.db.session import SessionFactory
from app.models.enums import ExecutionStatus, NodeExecutionStatus
from app.services.control import finalize_dead, recover_stale_executions
from app.services.events import EventPublisher
from app.services.runs import InfrastructureUnavailable, run_execution, utcnow
from app.services.task_queue import CeleryTaskQueue
from app.worker.celery_app import RUN_EXECUTION_TASK, celery_app

logger = logging.getLogger(__name__)

# Set when this worker boots (before the pool forks, so children inherit them): its node
# name (e.g. "worker@3f2a1c"), which identifies it on executions it claims, and the boot
# time -- executions it "owned" with an older heartbeat belonged to a previous
# incarnation and died with it.
_booted_at: datetime | None = None
_nodename: str | None = None


@contextlib.asynccontextmanager
async def worker_resources() -> AsyncIterator[tuple[SessionFactory, Redis]]:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    redis = new_redis()
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False), redis
    finally:
        await redis.aclose()
        await engine.dispose()


def consumed_queues() -> frozenset[str]:
    """The queues this worker consumes (`-Q`), as selected on the app before the pool forked."""
    names = frozenset(celery_app.amqp.queues.consume_from or {})
    return names or frozenset({celery_app.conf.task_default_queue})


async def _run(execution_id: uuid.UUID, worker_id: str, segment: int, queue: str | None) -> dict[str, Any]:
    async with worker_resources() as (session_factory, redis):
        return await run_execution(
            execution_id, session_factory=session_factory, redis=redis, worker_id=worker_id,
            segment=segment, queues=consumed_queues(), queue=queue, task_queue=CeleryTaskQueue(),
        )


async def _fail(execution_id: uuid.UUID, error: str) -> None:
    async with worker_resources() as (session_factory, redis), session_factory() as db:
        await finalize_dead(
            db, EventPublisher(redis), execution_id, ExecutionStatus.FAILED, error,
            running_status=NodeExecutionStatus.FAILED, running_reason=f"Interrupted: {error}",
            pending_reason=f"Not run: {error}",
        )


@celery_app.task(name=RUN_EXECUTION_TASK, bind=True, acks_late=True)
def run_execution_task(self: Any, execution_id: str, segment: int = 0) -> dict[str, Any]:
    """Run one segment of an execution: the nodes this worker's queues cover, then hand off."""
    # request.hostname is the machine name under some pools; prefer the node name.
    worker_id = _nodename or self.request.hostname or f"celery@{socket.gethostname()}"
    queue = (self.request.delivery_info or {}).get("routing_key")
    try:
        return asyncio.run(_run(uuid.UUID(execution_id), worker_id, segment, queue))
    except InfrastructureUnavailable as exc:
        # Only raised before the run was claimed, so retrying can't repeat any node.
        retries = self.request.retries
        if retries >= settings.CELERY_TASK_MAX_RETRIES:
            logger.error("giving up: infrastructure unavailable", extra={"execution_id": execution_id, "error": str(exc)})
            raise
        countdown = min(60, 5 * 2**retries)
        logger.warning(
            "infrastructure unavailable; retrying the task",
            extra={"execution_id": execution_id, "retry": retries + 1, "countdown": countdown, "error": str(exc)},
        )
        raise self.retry(exc=exc, countdown=countdown, max_retries=settings.CELERY_TASK_MAX_RETRIES) from exc
    except SoftTimeLimitExceeded:
        error = f"Hit the worker's hard time limit ({settings.EXECUTION_TIME_LIMIT_SECONDS + 30:g}s)"
        logger.error("task time limit", extra={"execution_id": execution_id})
        with contextlib.suppress(Exception):
            asyncio.run(_fail(uuid.UUID(execution_id), error))
        raise


@signals.worker_init.connect
def _record_boot(sender: Any = None, **_: object) -> None:
    global _booted_at, _nodename
    _booted_at = utcnow()
    _nodename = getattr(sender, "hostname", None)


@signals.worker_ready.connect
def _recover_on_start(sender: Any = None, **_: object) -> None:
    """Fail executions a previous incarnation of this worker left 'running'."""
    hostname = getattr(sender, "hostname", None) or _nodename

    async def recover() -> list[uuid.UUID]:
        async with worker_resources() as (session_factory, redis), session_factory() as db:
            return await recover_stale_executions(db, redis, restarted_worker=hostname, restarted_before=_booted_at)

    try:
        recovered = asyncio.run(recover())
    except Exception:
        logger.exception("startup recovery failed")
        return
    logger.info("worker ready", extra={
        "worker": hostname, "queues": sorted(consumed_queues()), "recovered_executions": [str(i) for i in recovered],
    })
