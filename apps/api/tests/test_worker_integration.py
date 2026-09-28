"""The real dispatch path: CeleryTaskQueue -> Redis broker (db 15) -> a Celery worker
(celery.contrib.testing, in a thread, 'solo' pool) -> run_execution -> Postgres + events.

Unlike the other API tests, rows here are committed (the worker has its own connections)
and deleted afterwards.
"""

import asyncio
import uuid

import pytest
from celery import signals
from celery.contrib.testing.worker import start_worker
from flowforge_engine import ExecutionServices, ProviderSettings, WorkflowGraph
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.security import hash_password
from app.models.enums import ExecutionStatus
from app.models.execution import WorkflowExecution
from app.models.user import User
from app.models.workflow import Workflow
from app.schemas.workflow import EXAMPLE_GRAPH
from app.services.control import stop_execution
from app.services.runs import TERMINAL_STATUSES, create_execution, load_execution_detail
from app.services.task_queue import CeleryTaskQueue
from app.services.workflows import replace_graph
from app.worker import tasks  # noqa: F401  (registers the task on celery_app)
from app.worker.celery_app import _configure_logging, celery_app
from tests.support import collect_events, delay_graph, kinds

WORKER = "testworker@flowforge"


@pytest.fixture(scope="module")
def celery_worker(migrated_database):
    # Keep pytest's logging setup: don't let the worker install the JSON handlers.
    signals.setup_logging.disconnect(_configure_logging)
    try:
        with start_worker(
            celery_app, pool="solo", concurrency=1, perform_ping_check=False,
            hostname=WORKER, queues=["default"], shutdown_timeout=30,
        ) as worker:
            yield worker
    finally:
        signals.setup_logging.connect(_configure_logging)


@pytest.fixture
async def committed(migrated_database):
    """Sessions on real, committed transactions; the user (and everything of theirs) is
    deleted afterwards."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as db:
        user = User(email=f"worker-{uuid.uuid4().hex[:8]}@example.com", hashed_password=hash_password("password123"),
                    full_name="Worker Test")
        db.add(user)
        await db.commit()
    yield factory, user
    async with factory() as db:
        await db.delete(await db.get(User, user.id))
        await db.commit()
    await engine.dispose()


async def queue(factory, user, graph, inputs=None):
    async with factory() as db:
        workflow = Workflow(name="worker test", owner_id=user.id, graph_json={})
        db.add(workflow)
        await db.flush()
        await replace_graph(db, workflow, WorkflowGraph.model_validate(graph))
        await db.commit()
        services = ExecutionServices(provider_settings=ProviderSettings(testing=True))
        execution = await create_execution(db, workflow, user, inputs or {}, services, queue="default")
    return execution.id


async def wait_terminal(factory, user, execution_id, timeout=20):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        async with factory() as db:
            detail = await load_execution_detail(db, execution_id, user)
        if detail.status in TERMINAL_STATUSES or loop.time() > deadline:
            return detail
        await asyncio.sleep(0.1)


async def test_celery_worker_runs_the_queued_execution(celery_worker, committed, redis):
    factory, user = committed
    execution_id = await queue(factory, user, EXAMPLE_GRAPH, {"topic": "tides"})
    async with collect_events(redis, execution_id) as collector:
        task_id = await CeleryTaskQueue().enqueue(execution_id, "default")
        await collector.until("execution.finished", timeout=20)
        events = await collector.drain()
    detail = await wait_terminal(factory, user, execution_id)

    assert task_id == str(execution_id)
    assert detail.status is ExecutionStatus.SUCCESS and detail.worker_hostname == WORKER
    assert [n.status.value for n in detail.node_executions] == ["success"] * 4
    assert kinds(events)[0] == ("execution.started", None) and kinds(events)[-1] == ("execution.finished", None)
    assert events[0]["worker"] == WORKER
    assert celery_app.AsyncResult(task_id).get(timeout=10)["status"] == "success"


async def test_enqueueing_twice_runs_once(celery_worker, committed, redis):
    factory, user = committed
    execution_id = await queue(factory, user, EXAMPLE_GRAPH, {"topic": "tides"})
    async with collect_events(redis, execution_id) as collector:
        await CeleryTaskQueue().enqueue(execution_id, "default")
        await CeleryTaskQueue().enqueue(execution_id, "default")
        await collector.until("execution.finished", timeout=20)
        await asyncio.sleep(1.5)  # time for the second delivery to be processed
        events = await collector.drain()
    detail = await wait_terminal(factory, user, execution_id)

    assert detail.status is ExecutionStatus.SUCCESS
    assert [e["type"] for e in events].count("execution.started") == 1
    assert [e["type"] for e in events].count("execution.finished") == 1
    assert len(detail.node_executions) == 4


async def test_stop_through_the_real_worker(celery_worker, committed, redis):
    factory, user = committed
    execution_id = await queue(factory, user, delay_graph(30))
    async with collect_events(redis, execution_id) as collector:
        await CeleryTaskQueue().enqueue(execution_id, "default")
        await collector.until("node.started", "wait", timeout=20)
        async with factory() as db:
            outcome = await stop_execution(db, execution_id, redis=redis, task_queue=CeleryTaskQueue(), wait_seconds=5)
        await collector.until("execution.finished", timeout=10)
    detail = await wait_terminal(factory, user, execution_id)

    assert outcome.finished and outcome.how == "stopped by the worker"
    assert detail.status is ExecutionStatus.STOPPED
    nodes = {n.node_key: n for n in detail.node_executions}
    assert nodes["wait"].status.value == "skipped" and nodes["wait"].error_message.startswith("Interrupted after")
    assert nodes["out"].status.value == "skipped"


async def test_execution_rows_carry_dispatch_details(celery_worker, committed):
    factory, user = committed
    execution_id = await queue(factory, user, EXAMPLE_GRAPH, {"topic": "tides"})
    await CeleryTaskQueue().enqueue(execution_id, "default")
    await wait_terminal(factory, user, execution_id)
    async with factory() as db:
        row = await db.get(WorkflowExecution, execution_id)
    assert row.queue == "default" and row.celery_task_id == str(execution_id)
    assert row.heartbeat_at is not None and row.inputs_json == {"topic": "tides"}
