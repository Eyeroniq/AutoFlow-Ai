"""Dispatching runs to Celery. The API only *sends* tasks (by name); it never imports the
worker's task code, and tests swap this for a recording fake."""

import asyncio
import uuid
from typing import Protocol

from app.worker.celery_app import INGEST_DOCUMENT_TASK, RUN_EXECUTION_TASK, celery_app

# Knowledge-base ingestion reads files (OCR for scans), so it runs on the OCR workers.
INGEST_QUEUE = "ocr"


class EnqueueFailed(Exception):
    """The broker couldn't be reached, so the run was not queued."""


class TaskQueue(Protocol):
    async def enqueue(self, execution_id: uuid.UUID, queue: str, *, segment: int = 0) -> str:
        """Queue a run (segment 0) or the continuation of a handed-off one; returns the task id."""
        ...

    async def revoke(self, task_id: str) -> None: ...


def task_id_for(execution_id: uuid.UUID, segment: int = 0) -> str:
    """The run's first task is the execution id; continuations are "<id>:<segment>"."""
    return str(execution_id) if segment == 0 else f"{execution_id}:{segment}"


class CeleryTaskQueue:
    async def enqueue(self, execution_id: uuid.UUID, queue: str, *, segment: int = 0) -> str:
        # kombu's publish is blocking network I/O; keep it off the event loop.
        try:
            return await asyncio.to_thread(self._send, execution_id, queue, segment)
        except Exception as exc:
            raise EnqueueFailed(f"{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _send(execution_id: uuid.UUID, queue: str, segment: int) -> str:
        # Deterministic task ids make re-sent tasks recognizable, and the worker's claim
        # (a compare-and-set per segment) guarantees each segment runs at most once.
        result = celery_app.send_task(
            RUN_EXECUTION_TASK,
            args=[str(execution_id), segment],
            task_id=task_id_for(execution_id, segment),
            queue=queue,
            retry=True,
            retry_policy={"max_retries": 2, "interval_start": 0.2, "interval_step": 0.5, "interval_max": 1},
        )
        return result.id

    async def revoke(self, task_id: str) -> None:
        await asyncio.to_thread(celery_app.control.revoke, task_id)


class IngestQueue(Protocol):
    async def enqueue_ingest(self, document_id: uuid.UUID) -> str:
        """Queue a knowledge-base document for reading and embedding; returns the task id."""
        ...


class CeleryIngestQueue:
    async def enqueue_ingest(self, document_id: uuid.UUID) -> str:
        try:
            return await asyncio.to_thread(self._send, document_id)
        except Exception as exc:
            raise EnqueueFailed(f"{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _send(document_id: uuid.UUID) -> str:
        # The task skips documents that aren't pending, so a re-sent task is harmless.
        result = celery_app.send_task(
            INGEST_DOCUMENT_TASK, args=[str(document_id)], task_id=f"ingest:{document_id}", queue=INGEST_QUEUE,
            retry=True, retry_policy={"max_retries": 2, "interval_start": 0.2, "interval_step": 0.5, "interval_max": 1},
        )
        return result.id


_queue = CeleryTaskQueue()
_ingest_queue = CeleryIngestQueue()


def get_task_queue() -> TaskQueue:
    return _queue


def get_ingest_queue() -> IngestQueue:
    return _ingest_queue
