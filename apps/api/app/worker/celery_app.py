"""The Celery application: Redis broker + result backend, JSON only, late acks.

Start a worker (the `worker` Compose service does this):

    celery -A app.worker.celery_app:celery_app worker -Q default --hostname worker@%h

The API imports this module only to *send* tasks by name; the task code lives in
app.worker.tasks, which only workers load (via `include`).
"""

from celery import Celery, signals
from flowforge_engine import DEFAULT_QUEUE

from app.core.config import settings
from app.core.logging import register_secret, setup_logging

RUN_EXECUTION_TASK = "flowforge.run_execution"

celery_app = Celery(
    "flowforge",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Runs are routed per queue (flowforge_engine.queue_for_graph); workers consume
    # "default" unless started with other -Q queues.
    task_default_queue=DEFAULT_QUEUE,
    task_create_missing_queues=True,
    # Ack only after the task finishes, and take one task at a time: a worker that dies
    # before or while running doesn't lose the message. Redelivery is harmless because
    # the task claims its execution with a compare-and-set.
    task_acks_late=True,
    task_reject_on_worker_lost=False,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    # The run enforces EXECUTION_TIME_LIMIT_SECONDS itself (cooperatively, recording
    # results); Celery's limits are backstops above it.
    task_soft_time_limit=settings.EXECUTION_TIME_LIMIT_SECONDS + 30,
    task_time_limit=settings.EXECUTION_TIME_LIMIT_SECONDS + 60,
    # Longer than any run, so Redis doesn't redeliver a task that is still running.
    broker_transport_options={"visibility_timeout": int(settings.EXECUTION_TIME_LIMIT_SECONDS + 300)},
    broker_connection_retry_on_startup=True,
    result_expires=24 * 3600,
    worker_hijack_root_logger=False,
)


@signals.setup_logging.connect
def _configure_logging(**_: object) -> None:
    """Workers log the same structured JSON as the API, with secrets redacted."""
    setup_logging(settings.LOG_LEVEL)
    for secret in settings.secret_values():
        register_secret(secret)
