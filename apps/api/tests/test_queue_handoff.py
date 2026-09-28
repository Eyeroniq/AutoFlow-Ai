"""A run spread over workers of different queues: hand-off, claim, resume, stop and recovery
while waiting between queues. The worker code runs in-process as two "workers" with
different queue sets; the fake task queue records the continuation each hand-off sends."""

from datetime import timedelta

from sqlalchemy import select, update

from app.core.config import settings
from app.models.enums import ExecutionStatus, NodeExecutionStatus
from app.models.execution import NodeExecution, WorkflowExecution
from app.services.control import recover_stale_executions
from app.services.runs import run_execution, utcnow
from tests.support import collect_events, create_workflow, kinds, queue_run

LLM_WORKER, DEFAULT_WORKER = "worker-llm@a1", "worker-default@b2"


def mixed_graph():
    """Input (portable) -> Gemini (llm) -> Delay (default) -> Output (portable)."""
    return {
        "nodes": [
            {"id": "input", "type": "input", "config": {"name": "topic"}},
            {"id": "llm", "type": "gemini", "config": {"provider": "mock", "user_prompt": "About {{input.topic}}"}},
            {"id": "wait", "type": "delay", "config": {"seconds": 0}},
            {"id": "out", "type": "output", "config": {"value": {"answer": "{{llm.response}}"}}},
        ],
        "edges": [{"source": "input", "target": "llm"}, {"source": "llm", "target": "wait"},
                  {"source": "wait", "target": "out"}],
    }


def segment(execution_id, session_factory, redis, task_queue, *, worker, queue, number=0):
    return run_execution(
        execution_id, session_factory=session_factory, redis=redis, worker_id=worker,
        segment=number, queues={queue}, queue=queue, task_queue=task_queue,
    )


async def rows(shared_session, execution_id):
    result = await shared_session.scalars(
        select(NodeExecution).where(NodeExecution.execution_id == execution_id).execution_options(populate_existing=True)
    )
    return {row.node_key: row for row in result}


async def queued(client, user):
    wid = await create_workflow(client, user, mixed_graph())
    return await queue_run(client, user, wid, {"topic": "tides"})


async def test_a_run_moves_between_queues(client, user, task_queue, session_factory, redis, shared_session):
    execution_id = await queued(client, user)
    assert task_queue.enqueued == [(execution_id, "llm")]

    async with collect_events(redis, execution_id) as collector:
        first = await segment(execution_id, session_factory, redis, task_queue, worker=LLM_WORKER, queue="llm")
        events = await collector.drain()
    assert first == {"execution_id": str(execution_id), "ran": True, "segment": 0, "status": "running",
                     "handed_off_to": "default"}
    assert task_queue.continued == [(execution_id, "default", 1)]
    assert kinds(events) == [
        ("execution.started", None), ("node.started", "input"), ("node.succeeded", "input"),
        ("node.started", "llm"), ("node.succeeded", "llm"), ("execution.handoff", None),
    ]
    handoff = events[-1]
    assert handoff["from_queue"] == "llm" and handoff["to_queue"] == "default" and handoff["segment"] == 1

    # Waiting for a default worker: still running, owned by no one.
    row = await shared_session.get(WorkflowExecution, execution_id, populate_existing=True)
    assert row.status is ExecutionStatus.RUNNING and row.segment == 1 and row.queue == "default"
    assert row.worker_hostname is None and row.heartbeat_at is None and row.handoff_at is not None
    nodes = await rows(shared_session, execution_id)
    assert (nodes["llm"].status, nodes["llm"].queue, nodes["llm"].worker_hostname) == \
        (NodeExecutionStatus.SUCCESS, "llm", LLM_WORKER)
    assert nodes["wait"].status is NodeExecutionStatus.PENDING

    async with collect_events(redis, execution_id) as collector:
        second = await segment(execution_id, session_factory, redis, task_queue,
                               worker=DEFAULT_WORKER, queue="default", number=1)
        events = await collector.drain()
    assert second["status"] == "success" and second["segment"] == 1
    assert kinds(events) == [
        ("execution.resumed", None), ("node.started", "wait"), ("node.succeeded", "wait"),
        ("node.started", "out"), ("node.succeeded", "out"), ("execution.finished", None),
    ]
    assert events[0]["worker"] == DEFAULT_WORKER and events[0]["queue"] == "default"

    detail = (await client.get(f"/api/executions/{execution_id}", headers=user.headers)).json()
    assert detail["status"] == "success" and detail["segment"] == 1 and detail["handoff_at"] is None
    # The output resolved Gemini's answer from the row the first worker wrote.
    assert "tides" in detail["final_output"]["result"]["answer"]
    ran_on = {n["node_key"]: (n["queue"], n["worker_hostname"]) for n in detail["node_executions"]}
    assert ran_on == {"input": ("llm", LLM_WORKER), "llm": ("llm", LLM_WORKER),
                      "wait": ("default", DEFAULT_WORKER), "out": ("default", DEFAULT_WORKER)}


async def test_each_segment_runs_once(client, user, task_queue, session_factory, redis):
    execution_id = await queued(client, user)
    await segment(execution_id, session_factory, redis, task_queue, worker=LLM_WORKER, queue="llm")
    again = await segment(execution_id, session_factory, redis, task_queue, worker="worker-llm@other", queue="llm")
    assert again["ran"] is False  # segment 0 was already claimed

    first = await segment(execution_id, session_factory, redis, task_queue, worker=DEFAULT_WORKER, queue="default", number=1)
    duplicate = await segment(execution_id, session_factory, redis, task_queue, worker="worker-default@x",
                              queue="default", number=1)
    assert first["status"] == "success" and duplicate["ran"] is False
    stale = await segment(execution_id, session_factory, redis, task_queue, worker=DEFAULT_WORKER, queue="default", number=2)
    assert stale["ran"] is False


async def test_a_worker_serving_every_queue_never_hands_off(client, user, task_queue, session_factory, redis):
    execution_id = await queued(client, user)
    summary = await run_execution(execution_id, session_factory=session_factory, redis=redis, worker_id="worker@all",
                                  queues={"default", "llm", "ocr"}, queue="llm", task_queue=task_queue)
    assert summary["status"] == "success" and task_queue.continued == []


async def test_stopping_a_run_that_is_waiting_between_queues(client, user, task_queue, session_factory, redis):
    execution_id = await queued(client, user)
    await segment(execution_id, session_factory, redis, task_queue, worker=LLM_WORKER, queue="llm")

    response = await client.post(f"/api/executions/{execution_id}/stop", headers=user.headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "stopped" and body["error_message"] == "Stopped by user while waiting for a 'default' worker"
    nodes = {n["node_key"]: n for n in body["node_executions"]}
    assert nodes["llm"]["status"] == "success" and nodes["wait"]["status"] == "skipped"

    late = await segment(execution_id, session_factory, redis, task_queue, worker=DEFAULT_WORKER, queue="default", number=1)
    assert late["ran"] is False


async def test_a_hand_off_nobody_picks_up_is_failed(client, user, task_queue, session_factory, redis, shared_session):
    execution_id = await queued(client, user)
    await segment(execution_id, session_factory, redis, task_queue, worker=LLM_WORKER, queue="llm")
    # A fresh hand-off is left alone...
    assert execution_id not in await recover_stale_executions(shared_session, redis)
    # ...one older than the pending timeout means no worker serves that queue.
    old = utcnow() - timedelta(seconds=settings.EXECUTION_PENDING_TIMEOUT_SECONDS + 5)
    await shared_session.execute(update(WorkflowExecution).where(WorkflowExecution.id == execution_id).values(handoff_at=old))
    await shared_session.commit()
    assert execution_id in await recover_stale_executions(shared_session, redis)
    detail = (await client.get(f"/api/executions/{execution_id}", headers=user.headers)).json()
    assert detail["status"] == "failed"
    assert detail["error_message"].startswith("No worker for the 'default' queue picked this run up within")


async def test_a_failed_hand_off_fails_the_run(client, user, task_queue, session_factory, redis):
    execution_id = await queued(client, user)
    task_queue.fail = True  # the broker is down when the worker tries to hand off
    summary = await segment(execution_id, session_factory, redis, task_queue, worker=LLM_WORKER, queue="llm")
    assert summary["handed_off_to"] == "default"
    detail = (await client.get(f"/api/executions/{execution_id}", headers=user.headers)).json()
    assert detail["status"] == "failed"
    assert "Could not hand the run off to the 'default' queue" in detail["error_message"]
    assert {n["node_key"]: n["status"] for n in detail["node_executions"]}["wait"] == "skipped"
