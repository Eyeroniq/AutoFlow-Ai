"""Dispatching runs to Celery. The API only *sends* tasks (by name); it never imports the
worker's task code, and tests swap this for a recording fake."""

import asyncio
import uuid
from typing import Protocol

from app.worker.celery_app import RUN_EXECUTION_TASK, celery_app


class EnqueueFailed(Exception):
    """The broker couldn't be reached, so the run was not queued."""


class TaskQueue(Protocol):
    async def enqueue(self, execution_id: uuid.UUID, queue: str) -> str:
        """Queue a run; returns the Celery task id (the execution id)."""
        ...

    async def revoke(self, task_id: str) -> None: ...


class CeleryTaskQueue:
    async def enqueue(self, execution_id: uuid.UUID, queue: str) -> str:
        # kombu's publish is blocking network I/O; keep it off the event loop.
        try:
            return await asyncio.to_thread(self._send, execution_id, queue)
        except Exception as exc:
            raise EnqueueFailed(f"{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _send(execution_id: uuid.UUID, queue: str) -> str:
        # task_id = execution id: a re-sent task is recognizable, and the worker's claim
        # (pending -> running compare-and-set) guarantees it runs at most once.
        result = celery_app.send_task(
            RUN_EXECUTION_TASK,
            args=[str(execution_id)],
            task_id=str(execution_id),
            queue=queue,
            retry=True,
            retry_policy={"max_retries": 2, "interval_start": 0.2, "interval_step": 0.5, "interval_max": 1},
        )
        return result.id

    async def revoke(self, task_id: str) -> None:
        await asyncio.to_thread(celery_app.control.revoke, task_id)


_queue = CeleryTaskQueue()


def get_task_queue() -> TaskQueue:
    return _queue
