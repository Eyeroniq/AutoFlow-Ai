"""Queued runs: 202 + enqueue, the worker's state machine and events, stop, and recovery.

These run the worker's code (app.services.runs.run_execution) in-process against the test
database and a real Redis (db 15): no Celery process. tests/test_worker_integration.py
covers the same path through a real Celery worker.
"""

import asyncio
import time
import uuid
from datetime import timedelta

from sqlalchemy import select, update

from app.core.config import settings
from app.models.enums import ExecutionStatus, NodeExecutionStatus
from app.models.execution import NodeExecution, WorkflowExecution
from app.schemas.workflow import EXAMPLE_GRAPH
from app.services.control import recover_stale_executions
from app.services.events import EventPublisher, current_seq, stop_flag_set
from app.services.runs import claim_execution, run_execution, utcnow
from tests.support import collect_events, create_workflow, delay_graph, kinds, queue_run

EXAMPLE_ORDER = ["input", "gemini", "gmail", "output"]


async def get_execution(client, user, execution_id):
    response = await client.get(f"/api/executions/{execution_id}", headers=user.headers)
    assert response.status_code == 200, response.text
    return response.json()


def run(execution_id, session_factory, redis, worker_id="worker@test"):
    return run_execution(execution_id, session_factory=session_factory, redis=redis, worker_id=worker_id)


# --- dispatch ---------------------------------------------------------------------------


async def test_run_returns_202_immediately_and_queues_the_execution(client, user, task_queue):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    start = time.perf_counter()
    response = await client.post(f"/api/workflows/{wid}/run", json={"inputs": {"topic": "tides"}}, headers=user.headers)
    elapsed = time.perf_counter() - start

    assert response.status_code == 202
    body = response.json()
    execution_id = body["execution_id"]
    assert body["status"] == "pending" and body["queue"] == "default" and body["workflow_id"] == wid
    assert body["links"] == {
        "execution": f"/api/executions/{execution_id}",
        "events": f"/ws/executions/{execution_id}",
        "stop": f"/api/executions/{execution_id}/stop",
    }
    assert task_queue.enqueued == [(uuid.UUID(execution_id), "default")]
    assert elapsed < 2  # nothing ran in the request

    execution = await get_execution(client, user, execution_id)
    assert execution["status"] == "pending" and execution["started_at"] is None
    assert execution["inputs"] == {"topic": "tides"} and execution["queue"] == "default"
    assert [(n["node_key"], n["status"]) for n in execution["node_executions"]] == [
        (key, "pending") for key in EXAMPLE_ORDER
    ]


async def test_invalid_graph_is_rejected_before_queueing(client, user, task_queue):
    wid = await create_workflow(client, user, {"nodes": [{"id": "x", "type": "nope"}]})
    response = await client.post(f"/api/workflows/{wid}/run", headers=user.headers)
    assert response.status_code == 422
    assert task_queue.enqueued == []
    assert (await client.get(f"/api/workflows/{wid}/executions", headers=user.headers)).json() == []


async def test_broker_down_returns_503_and_fails_the_execution(client, user, task_queue):
    task_queue.fail = True
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    response = await client.post(f"/api/workflows/{wid}/run", headers=user.headers)
    assert response.status_code == 503
    assert "task broker is unavailable" in response.json()["detail"]
    [summary] = (await client.get(f"/api/workflows/{wid}/executions", headers=user.headers)).json()
    execution = await get_execution(client, user, summary["id"])
    assert execution["status"] == "failed" and "Could not queue the run" in execution["error_message"]
    assert {n["status"] for n in execution["node_executions"]} == {"skipped"}


async def test_run_is_routed_to_the_queue_its_nodes_need(client, user, task_queue, monkeypatch):
    from app.api.routes import workflows

    monkeypatch.setattr(workflows, "queue_for_graph", lambda graph: "gpu")
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    execution_id = await queue_run(client, user, wid, {"topic": "x"})
    assert task_queue.enqueued == [(execution_id, "gpu")]
    assert (await get_execution(client, user, execution_id))["queue"] == "gpu"


# --- the worker's state machine and events ------------------------------------------------


async def test_worker_records_states_and_publishes_events_in_order(
    client, user, session_factory, redis, shared_session, monkeypatch
):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    execution_id = await queue_run(client, user, wid, {"topic": "solar power"})

    # Record the database state at the moment each event is published (i.e. after the
    # state change was committed).
    states = []
    original = EventPublisher.publish

    async def publish_and_look(self, eid, event_type, **fields):
        seq = await original(self, eid, event_type, **fields)
        execution_status = await shared_session.scalar(select(WorkflowExecution.status).where(WorkflowExecution.id == eid))
        node_status = None
        if fields.get("node_key"):
            node_status = await shared_session.scalar(select(NodeExecution.status).where(
                NodeExecution.execution_id == eid, NodeExecution.node_key == fields["node_key"]))
        states.append((event_type, fields.get("node_key"), execution_status.value, node_status and node_status.value))
        return seq

    monkeypatch.setattr(EventPublisher, "publish", publish_and_look)

    async with collect_events(redis, execution_id) as collector:
        summary = await run(execution_id, session_factory, redis)
        events = await collector.drain()

    assert summary == {"execution_id": str(execution_id), "ran": True, "status": "success", "error": None}
    expected = [("execution.started", None)]
    for key in EXAMPLE_ORDER:
        expected += [("node.started", key), ("node.succeeded", key)]
    expected.append(("execution.finished", None))
    assert kinds(events) == expected

    # seq is gapless and increasing; every event is stamped and addressed.
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert await current_seq(redis, execution_id) == len(events)
    assert all(e["execution_id"] == str(execution_id) and e["timestamp"] for e in events)
    started, finished = events[0], events[-1]
    assert started["status"] == "running" and started["worker"] == "worker@test" and started["queue"] == "default"
    assert [n["node_key"] for n in started["nodes"]] == EXAMPLE_ORDER
    assert finished["status"] == "success" and finished["error"] is None
    assert finished["final_output"]["result"]["summary"].startswith("[MOCK RESPONSE to: Write a three-sentence")
    succeeded = next(e for e in events if e["type"] == "node.succeeded" and e["node_key"] == "gemini")
    assert succeeded["output"]["provider_used"] == "gemini" and succeeded["duration_ms"] is not None
    assert succeeded["input"]["user_prompt"] == "Write a three-sentence summary of solar power."

    # The database moved pending -> running -> success for the run and for each node.
    assert states[0] == ("execution.started", None, "running", None)
    for key in EXAMPLE_ORDER:
        assert ("node.started", key, "running", "running") in states
        assert ("node.succeeded", key, "running", "success") in states
    assert states[-1] == ("execution.finished", None, "success", None)

    execution = await get_execution(client, user, execution_id)
    assert execution["status"] == "success" and execution["worker_hostname"] == "worker@test"
    assert execution["started_at"] <= execution["finished_at"]
    for node in execution["node_executions"]:
        assert node["status"] == "success" and node["started_at"] and node["finished_at"] and node["duration_ms"] is not None


async def test_node_failure_is_recorded_not_retried(client, user, session_factory, redis):
    graph = {**EXAMPLE_GRAPH, "variables": [{"key": "recipient", "value": "not-an-email", "type": "workflow"}]}
    wid = await create_workflow(client, user, graph)
    execution_id = await queue_run(client, user, wid, {"topic": "x"})
    async with collect_events(redis, execution_id) as collector:
        summary = await run(execution_id, session_factory, redis)
        events = await collector.drain()

    assert summary["ran"] and summary["status"] == "failed"
    assert kinds(events)[-3:] == [("node.failed", "gmail"), ("node.skipped", "output"), ("execution.finished", None)]
    failed = next(e for e in events if e["type"] == "node.failed")
    assert failed["error"] == "Invalid email address(es): not-an-email"
    assert events[-1]["status"] == "failed" and events[-1]["error"].startswith("Node 'gmail' failed")


async def test_a_second_delivery_does_not_run_it_again(client, user, session_factory, redis, shared_session):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    execution_id = await queue_run(client, user, wid, {"topic": "x"})
    async with collect_events(redis, execution_id) as collector:
        first = await run(execution_id, session_factory, redis)
        second = await run(execution_id, session_factory, redis, worker_id="worker@other")
        events = await collector.drain()

    assert first["ran"] is True and second == {"execution_id": str(execution_id), "ran": False, "status": "success"}
    assert [e["type"] for e in events].count("execution.started") == 1
    rows = (await shared_session.scalars(select(NodeExecution).where(NodeExecution.execution_id == execution_id))).all()
    assert len(rows) == len(EXAMPLE_ORDER)
    assert (await get_execution(client, user, execution_id))["worker_hostname"] == "worker@test"


async def test_llm_tokens_are_streamed_as_events(client, user, session_factory, redis):
    graph = {
        "nodes": [
            {"id": "llm", "type": "groq", "config": {"user_prompt": "Explain tides in simple words", "stream": True}},
            {"id": "out", "type": "output", "config": {"value": "{{llm.response}}"}},
        ],
        "edges": [{"source": "llm", "target": "out"}],
    }
    wid = await create_workflow(client, user, graph)
    execution_id = await queue_run(client, user, wid)
    async with collect_events(redis, execution_id) as collector:
        await run(execution_id, session_factory, redis)
        events = await collector.drain()

    types = [e["type"] for e in events]
    first_token, last_token = types.index("node.token"), len(types) - 1 - types[::-1].index("node.token")
    assert types.index("node.started") < first_token and last_token < types.index("node.succeeded")
    tokens = [e for e in events if e["type"] == "node.token"]
    assert all(t["node_key"] == "llm" and t["provider"] == "groq" for t in tokens)
    response = next(e for e in events if e["type"] == "node.succeeded")["output"]["response"]
    assert "".join(t["text"] for t in tokens) == response


async def test_credentials_removed_after_queueing_fail_the_run(client, user, session_factory, redis, monkeypatch):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    execution_id = await queue_run(client, user, wid, {"topic": "x"})
    monkeypatch.setattr(settings, "TESTING", False)
    for name in ("GEMINI_API_KEY", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.setattr(settings, name, None)

    summary = await run(execution_id, session_factory, redis)
    assert summary["status"] == "failed" and "Authentication missing for provider 'gemini'" in summary["error"]
    execution = await get_execution(client, user, execution_id)
    assert execution["status"] == "failed"
    assert {n["error_message"] for n in execution["node_executions"]} == {
        "Not run: the workflow failed validation when the run started"
    }


async def test_time_limit_stops_the_run_as_failed(client, user, session_factory, redis, monkeypatch):
    monkeypatch.setattr(settings, "EXECUTION_TIME_LIMIT_SECONDS", 0.3)
    wid = await create_workflow(client, user, delay_graph(30))
    execution_id = await queue_run(client, user, wid)
    start = time.perf_counter()
    summary = await run(execution_id, session_factory, redis)
    assert time.perf_counter() - start < 3
    assert summary["status"] == "failed" and summary["error"] == "Execution exceeded the time limit of 0.3s"
    nodes = {n["node_key"]: n for n in (await get_execution(client, user, execution_id))["node_executions"]}
    assert nodes["wait"]["status"] == "skipped" and nodes["wait"]["error_message"].startswith("Interrupted after")


# --- stop ------------------------------------------------------------------------------


async def test_stop_a_pending_execution(client, user, task_queue, session_factory, redis):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    execution_id = await queue_run(client, user, wid, {"topic": "x"})
    async with collect_events(redis, execution_id) as collector:
        response = await client.post(f"/api/executions/{execution_id}/stop", headers=user.headers)
        events = await collector.drain()

    assert response.status_code == 200
    execution = response.json()
    assert execution["status"] == "stopped" and execution["error_message"] == "Stopped by user before a worker started it"
    assert execution["stop_requested_at"] and execution["finished_at"]
    assert {n["status"] for n in execution["node_executions"]} == {"skipped"}
    assert task_queue.revoked == [str(execution_id)]
    assert kinds(events) == [("node.skipped", key) for key in EXAMPLE_ORDER] + [("execution.finished", None)]
    assert events[-1]["status"] == "stopped"

    # A late delivery of the revoked task does nothing.
    assert (await run(execution_id, session_factory, redis))["ran"] is False


async def test_stop_a_running_execution_mid_delay(client, user, session_factory, redis, monkeypatch):
    monkeypatch.setattr(settings, "EXECUTION_STOP_WAIT_SECONDS", 5)
    wid = await create_workflow(client, user, delay_graph(30))
    execution_id = await queue_run(client, user, wid)

    async with collect_events(redis, execution_id) as collector:
        runner = asyncio.create_task(run(execution_id, session_factory, redis))
        await collector.until("node.started", "wait")
        start = time.perf_counter()
        response = await client.post(f"/api/executions/{execution_id}/stop", headers=user.headers)
        stop_latency = time.perf_counter() - start
        summary = await asyncio.wait_for(runner, 5)
        events = await collector.drain()

    assert response.status_code == 200 and stop_latency < 3
    execution = response.json()
    assert execution["status"] == "stopped" and execution["error_message"] == "Stopped by user"
    nodes = {n["node_key"]: n for n in execution["node_executions"]}
    assert nodes["start"]["status"] == "success"
    assert nodes["wait"]["status"] == "skipped" and nodes["wait"]["error_message"].startswith("Interrupted after")
    assert nodes["wait"]["error_message"].endswith("Stopped by user")
    assert nodes["out"]["status"] == "skipped" and nodes["out"]["error_message"] == "Not run: Stopped by user"
    assert summary["status"] == "stopped"
    assert kinds(events)[-4:] == [
        ("node.started", "wait"), ("node.skipped", "wait"), ("node.skipped", "out"), ("execution.finished", None),
    ]
    assert events[-1]["status"] == "stopped"
    assert await stop_flag_set(redis, execution_id)


async def test_stop_returns_202_while_the_worker_hasnt_confirmed(client, user, session_factory, redis, shared_session):
    """No worker is consuming the stop flag here, so the API can only report 'requested'."""
    wid = await create_workflow(client, user, delay_graph(30))
    execution_id = await queue_run(client, user, wid)
    assert await claim_execution(shared_session, execution_id, "worker@busy")
    response = await client.post(f"/api/executions/{execution_id}/stop", headers=user.headers)
    assert response.status_code == 202
    assert response.json()["status"] == "running" and response.json()["stop_requested_at"]
    assert await stop_flag_set(redis, execution_id)


async def test_stop_with_a_dead_worker_is_finalized_by_the_api(client, user, redis, shared_session):
    wid = await create_workflow(client, user, delay_graph(30))
    execution_id = await queue_run(client, user, wid)
    assert await claim_execution(shared_session, execution_id, "worker@gone")
    await shared_session.execute(update(WorkflowExecution).where(WorkflowExecution.id == execution_id)
                                 .values(heartbeat_at=utcnow() - timedelta(minutes=5)))
    await shared_session.commit()
    response = await client.post(f"/api/executions/{execution_id}/stop", headers=user.headers)
    assert response.status_code == 200
    assert response.json()["status"] == "stopped"
    assert response.json()["error_message"] == "Stopped by user (its worker was unresponsive)"


async def test_stop_is_owner_only_and_not_after_finishing(client, user, user_factory, session_factory, redis):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    execution_id = await queue_run(client, user, wid, {"topic": "x"})
    stranger = await user_factory()
    assert (await client.post(f"/api/executions/{execution_id}/stop", headers=stranger.headers)).status_code == 404
    assert (await client.post(f"/api/executions/{uuid.uuid4()}/stop", headers=user.headers)).status_code == 404

    await run(execution_id, session_factory, redis)
    response = await client.post(f"/api/executions/{execution_id}/stop", headers=user.headers)
    assert response.status_code == 409 and "already finished with status 'success'" in response.json()["detail"]


# --- crash recovery -------------------------------------------------------------------------


async def running_execution(client, user, shared_session, *, worker="worker@dead", heartbeat_age=0.0):
    wid = await create_workflow(client, user, delay_graph(30))
    execution_id = await queue_run(client, user, wid)
    assert await claim_execution(shared_session, execution_id, worker)
    now = utcnow()
    await shared_session.execute(update(WorkflowExecution).where(WorkflowExecution.id == execution_id)
                                 .values(heartbeat_at=now - timedelta(seconds=heartbeat_age)))
    await shared_session.execute(update(NodeExecution).where(
        NodeExecution.execution_id == execution_id, NodeExecution.node_key == "start")
        .values(status=NodeExecutionStatus.SUCCESS, started_at=now, finished_at=now, duration_ms=1))
    await shared_session.execute(update(NodeExecution).where(
        NodeExecution.execution_id == execution_id, NodeExecution.node_key == "wait")
        .values(status=NodeExecutionStatus.RUNNING, started_at=now))
    await shared_session.commit()
    return execution_id


async def test_stale_heartbeat_marks_the_execution_failed(client, user, shared_session, redis):
    stale = await running_execution(client, user, shared_session, heartbeat_age=120)
    fresh = await running_execution(client, user, shared_session, worker="worker@alive", heartbeat_age=1)

    async with collect_events(redis, stale) as collector:
        recovered = await recover_stale_executions(shared_session, redis)
        events = await collector.drain()

    assert recovered == [stale]
    execution = await get_execution(client, user, stale)
    assert execution["status"] == "failed"
    assert execution["error_message"].startswith("No heartbeat from worker 'worker@dead' for 12")
    assert "crashed or was killed" in execution["error_message"]
    nodes = {n["node_key"]: n for n in execution["node_executions"]}
    assert nodes["start"]["status"] == "success"
    assert nodes["wait"]["status"] == "failed" and nodes["wait"]["error_message"].startswith("Interrupted: the worker stopped")
    assert nodes["out"]["status"] == "skipped"
    assert kinds(events) == [("node.failed", "wait"), ("node.skipped", "out"), ("execution.finished", None)]
    assert events[-1]["status"] == "failed"
    assert (await get_execution(client, user, fresh))["status"] == "running"  # a live worker is left alone


async def test_restarted_worker_recovers_its_own_runs_at_once(client, user, shared_session, redis):
    boot = utcnow()
    orphan = await running_execution(client, user, shared_session, worker="worker@box1", heartbeat_age=3)
    new_claim = await running_execution(client, user, shared_session, worker="worker@box1", heartbeat_age=-1)
    other_worker = await running_execution(client, user, shared_session, worker="worker@box2", heartbeat_age=3)

    recovered = await recover_stale_executions(shared_session, redis, restarted_worker="worker@box1", restarted_before=boot)
    assert recovered == [orphan]
    execution = await get_execution(client, user, orphan)
    assert execution["status"] == "failed"
    assert execution["error_message"].startswith("The worker 'worker@box1' restarted while this execution was running")
    assert (await get_execution(client, user, new_claim))["status"] == "running"
    assert (await get_execution(client, user, other_worker))["status"] == "running"


async def test_pending_that_no_worker_picks_up_expires(client, user, shared_session, redis, monkeypatch):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    execution_id = await queue_run(client, user, wid, {"topic": "x"})
    assert await recover_stale_executions(shared_session, redis) == []
    monkeypatch.setattr(settings, "EXECUTION_PENDING_TIMEOUT_SECONDS", 0.001)
    await asyncio.sleep(0.01)
    # created_at is the transaction start in Postgres (now()), so it's comfortably old.
    assert await recover_stale_executions(shared_session, redis) == [execution_id]
    execution = await get_execution(client, user, execution_id)
    assert execution["status"] == "failed" and "No worker picked this execution up" in execution["error_message"]


async def test_a_run_whose_execution_was_taken_over_stops_itself(
    client, user, session_factory, redis, shared_session, monkeypatch
):
    """If recovery (or a forced stop) finalizes the execution while the worker is actually
    still alive, the worker's next heartbeat notices and abandons the run."""
    monkeypatch.setattr(settings, "EXECUTION_HEARTBEAT_SECONDS", 0.05)
    wid = await create_workflow(client, user, delay_graph(30))
    execution_id = await queue_run(client, user, wid)
    async with collect_events(redis, execution_id) as collector:
        runner = asyncio.create_task(run(execution_id, session_factory, redis))
        await collector.until("node.started", "wait")
        await shared_session.execute(update(WorkflowExecution).where(WorkflowExecution.id == execution_id)
                                     .values(status=ExecutionStatus.FAILED, error_message="recovered elsewhere"))
        await shared_session.commit()
        await asyncio.wait_for(runner, 5)

    execution = await get_execution(client, user, execution_id)
    assert execution["status"] == "failed" and execution["error_message"] == "recovered elsewhere"


async def test_heartbeat_advances_while_running(client, user, session_factory, redis, shared_session, monkeypatch):
    monkeypatch.setattr(settings, "EXECUTION_HEARTBEAT_SECONDS", 0.05)
    wid = await create_workflow(client, user, delay_graph(0.4))
    execution_id = await queue_run(client, user, wid)
    runner = asyncio.create_task(run(execution_id, session_factory, redis))
    await asyncio.sleep(0.1)
    first = await shared_session.scalar(select(WorkflowExecution.heartbeat_at).where(WorkflowExecution.id == execution_id))
    await asyncio.sleep(0.2)
    second = await shared_session.scalar(select(WorkflowExecution.heartbeat_at).where(WorkflowExecution.id == execution_id))
    await runner
    assert first is not None and second > first

