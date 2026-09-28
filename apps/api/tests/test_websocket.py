"""WS /ws/executions/{id}: auth, ownership, snapshot + live events, replay, heartbeats, resync.

The socket runs in-process (httpx-ws ASGI transport) on the test database, with real Redis
pub/sub between the "worker" (run_execution in a background task) and the socket.
"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager

import pytest
from httpx import AsyncClient
from httpx_ws import WebSocketDisconnect, aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport

from app.core.config import settings
from app.main import app
from app.models.enums import ExecutionStatus
from app.schemas.workflow import EXAMPLE_GRAPH
from app.services.runs import finish_execution, run_execution
from tests.support import create_workflow, delay_graph, queue_run


@asynccontextmanager
async def connect(execution_id, token=None):
    """Open the socket in-process. (The ASGI transport runs a task group, so it must be
    entered and exited in the test's own task -- hence not a fixture.) Relies on the
    `client` fixture's dependency overrides being active."""
    address = f"http://test/ws/executions/{execution_id}" + (f"?token={token}" if token else "")
    async with AsyncClient(transport=ASGIWebSocketTransport(app=app), base_url="http://test") as http:
        async with aconnect_ws(address, http) as ws:
            yield ws


async def receive(ws, timeout=5):
    return json.loads(await ws.receive_text(timeout=timeout))


async def receive_until_closed(ws, timeout=5):
    messages = []
    with pytest.raises(WebSocketDisconnect) as closed:
        while True:
            messages.append(await receive(ws, timeout))
    return messages, closed.value


def start_run(execution_id, session_factory, redis):
    return asyncio.create_task(
        run_execution(execution_id, session_factory=session_factory, redis=redis, worker_id="worker@test")
    )


# --- auth and ownership ----------------------------------------------------------------------


async def test_missing_token_is_rejected(client, user, monkeypatch):
    monkeypatch.setattr(settings, "WS_AUTH_TIMEOUT_SECONDS", 0.2)
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    execution_id = await queue_run(client, user, wid, {"topic": "x"})
    async with connect(execution_id) as ws:
        messages, closed = await receive_until_closed(ws)
    assert closed.code == 4401
    assert messages == [{"type": "error", "code": 4401, "message": messages[0]["message"]}]
    assert "Not authenticated" in messages[0]["message"]


async def test_bad_tokens_are_rejected(client, user):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    execution_id = await queue_run(client, user, wid, {"topic": "x"})
    for token in ("garbage", user.refresh_token):  # a refresh token isn't an access token
        async with connect(execution_id, token) as ws:
            messages, closed = await receive_until_closed(ws)
        assert closed.code == 4401 and messages[0]["message"] == "Invalid or expired token"

    async with connect(execution_id) as ws:
        await ws.send_text(json.dumps({"hello": "there"}))
        messages, closed = await receive_until_closed(ws)
    assert closed.code == 4401 and "First message must be" in messages[0]["message"]


async def test_another_users_execution_is_not_found(client, user, user_factory):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    execution_id = await queue_run(client, user, wid, {"topic": "x"})
    stranger = await user_factory()
    for target, token in ((execution_id, stranger.access_token), (uuid.uuid4(), user.access_token)):
        async with connect(target, token) as ws:
            messages, closed = await receive_until_closed(ws)
        assert closed.code == 4404
        assert messages == [{"type": "error", "code": 4404, "message": "Execution not found"}]


async def test_first_message_auth(client, user):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    execution_id = await queue_run(client, user, wid, {"topic": "x"})
    async with connect(execution_id) as ws:
        await ws.send_text(json.dumps({"type": "auth", "token": user.access_token}))
        snapshot = await receive(ws)
    assert snapshot["type"] == "snapshot" and snapshot["execution"]["status"] == "pending"


# --- snapshot, live events, replay -------------------------------------------------------------


async def test_live_events_follow_the_snapshot_then_the_socket_closes(client, user, session_factory, redis):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    execution_id = await queue_run(client, user, wid, {"topic": "tides"})
    async with connect(execution_id, user.access_token) as ws:
        snapshot = await receive(ws)
        runner = start_run(execution_id, session_factory, redis)
        messages, closed = await receive_until_closed(ws)
        await runner

    assert snapshot["type"] == "snapshot" and snapshot["seq"] == 0 and snapshot["resync"] is False
    assert snapshot["execution"]["status"] == "pending"
    assert [n["node_key"] for n in snapshot["execution"]["node_executions"]] == ["input", "gemini", "gmail", "output"]

    types = [(m["type"], m.get("node_key")) for m in messages]
    expected = [("execution.started", None)]
    for key in ("input", "gemini", "gmail", "output"):
        expected += [("node.started", key), ("node.succeeded", key)]
    assert types == expected + [("execution.finished", None)]
    assert [m["seq"] for m in messages] == list(range(1, len(messages) + 1))
    assert messages[-1]["status"] == "success"
    assert closed.code == 1000


async def test_late_join_after_finish_replays_the_full_state(client, user, session_factory, redis):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    execution_id = await queue_run(client, user, wid, {"topic": "tides"})
    await run_execution(execution_id, session_factory=session_factory, redis=redis, worker_id="worker@test")

    async with connect(execution_id, user.access_token) as ws:
        messages, closed = await receive_until_closed(ws)

    snapshot, finished = messages
    assert snapshot["type"] == "snapshot" and snapshot["seq"] == 10
    execution = snapshot["execution"]
    assert execution["status"] == "success"
    assert [(n["node_key"], n["status"]) for n in execution["node_executions"]] == [
        ("input", "success"), ("gemini", "success"), ("gmail", "success"), ("output", "success"),
    ]
    assert execution["final_output"]["result"]["summary"].startswith("[MOCK RESPONSE")
    assert finished["type"] == "execution.finished" and finished["replayed"] is True
    assert finished["status"] == "success" and finished["final_output"] == execution["final_output"]
    assert closed.code == 1000


async def test_join_mid_run_gets_state_so_far_and_no_duplicates(client, user, session_factory, redis):
    wid = await create_workflow(client, user, delay_graph(0.5))
    execution_id = await queue_run(client, user, wid)
    runner = start_run(execution_id, session_factory, redis)
    await asyncio.sleep(0.2)  # into the delay
    async with connect(execution_id, user.access_token) as ws:
        snapshot = await receive(ws)
        messages, closed = await receive_until_closed(ws)
    await runner

    nodes = {n["node_key"]: n["status"] for n in snapshot["execution"]["node_executions"]}
    assert snapshot["execution"]["status"] == "running"
    assert nodes == {"start": "success", "wait": "running", "out": "pending"}
    # Only what happened after the snapshot, each once.
    assert [(m["type"], m.get("node_key")) for m in messages] == [
        ("node.succeeded", "wait"), ("node.started", "out"), ("node.succeeded", "out"), ("execution.finished", None),
    ]
    assert all(m["seq"] > snapshot["seq"] for m in messages)
    assert closed.code == 1000


async def test_stopped_execution_streams_to_the_end(client, user, session_factory, redis, monkeypatch):
    monkeypatch.setattr(settings, "EXECUTION_STOP_WAIT_SECONDS", 5)
    wid = await create_workflow(client, user, delay_graph(30))
    execution_id = await queue_run(client, user, wid)
    async with connect(execution_id, user.access_token) as ws:
        await receive(ws)  # snapshot
        runner = start_run(execution_id, session_factory, redis)
        message = {}
        while (message.get("type"), message.get("node_key")) != ("node.started", "wait"):
            message = await receive(ws)
        stop = await client.post(f"/api/executions/{execution_id}/stop", headers=user.headers)
        rest, closed = await receive_until_closed(ws)
        await runner
    assert stop.status_code == 200 and stop.json()["status"] == "stopped"
    assert [(m["type"], m.get("node_key")) for m in rest] == [
        ("node.skipped", "wait"), ("node.skipped", "out"), ("execution.finished", None),
    ]
    assert rest[-1]["status"] == "stopped" and closed.code == 1000


# --- keepalive and resilience ----------------------------------------------------------------


async def test_ping_pong_and_heartbeat(client, user, monkeypatch):
    monkeypatch.setattr(settings, "WS_HEARTBEAT_SECONDS", 0.1)
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    execution_id = await queue_run(client, user, wid, {"topic": "x"})
    async with connect(execution_id, user.access_token) as ws:
        assert (await receive(ws))["type"] == "snapshot"
        await ws.send_text(json.dumps({"type": "ping"}))
        seen = {(await receive(ws))["type"] for _ in range(3)}
    assert {"pong", "heartbeat"} <= seen


async def test_redis_drop_triggers_a_resync_snapshot(client, user, session_factory, redis, monkeypatch):
    from redis.asyncio.client import PubSub
    from redis.exceptions import ConnectionError as RedisConnectionError

    original = PubSub.get_message
    failures = {"left": 1}

    async def flaky(self, *args, **kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise RedisConnectionError("Connection reset by peer")
        return await original(self, *args, **kwargs)

    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    execution_id = await queue_run(client, user, wid, {"topic": "x"})
    monkeypatch.setattr(PubSub, "get_message", flaky)
    async with connect(execution_id, user.access_token) as ws:
        first = await receive(ws)
        resync = await receive(ws)
        runner = start_run(execution_id, session_factory, redis)
        messages, closed = await receive_until_closed(ws)
        await runner
    assert first["type"] == "snapshot" and first["resync"] is False
    assert resync["type"] == "snapshot" and resync["resync"] is True
    assert messages[-1]["type"] == "execution.finished" and closed.code == 1000


async def test_database_watchdog_closes_a_finished_run_whose_event_was_missed(
    client, user, shared_session, monkeypatch
):
    monkeypatch.setattr(settings, "WS_DB_CHECK_SECONDS", 0.1)
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    execution_id = await queue_run(client, user, wid, {"topic": "x"})
    async with connect(execution_id, user.access_token) as ws:
        assert (await receive(ws))["type"] == "snapshot"
        # Finish it without publishing anything (as if Redis had been down).
        await finish_execution(shared_session, None, execution_id, ExecutionStatus.FAILED, error="lost event",
                               from_statuses=(ExecutionStatus.PENDING,))
        messages, closed = await receive_until_closed(ws)
    snapshot, finished = messages
    assert snapshot["type"] == "snapshot" and snapshot["resync"] is True
    assert finished["replayed"] is True and finished["status"] == "failed" and finished["error"] == "lost event"
    assert closed.code == 1000
