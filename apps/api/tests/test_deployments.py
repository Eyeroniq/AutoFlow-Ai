"""Deployments: deploying (snapshot, key issued once and stored hashed, redeploy, rotation),
ownership, and the API-key endpoint: auth failures, async and ?wait=true runs, status
polling, and the per-deployment rate limit.

Runs execute in-process (app.services.runs.run_execution) against the test database and
the real Redis, like test_async_runs.py. Wait-mode tests swap the task queue for one that
starts the run as soon as the request queues it, so the request really waits on the
execution's event channel.
"""

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.deployment import Deployment
from app.models.execution import WorkflowExecution
from app.services import deployments as deployment_service
from app.services.runs import run_execution
from app.services.task_queue import get_task_queue
from tests.support import create_workflow, delay_graph, queue_run


def greeting_graph(text: str = "Hello {{input.topic}}") -> dict[str, Any]:
    """Input(topic) -> Text -> Output(result): runs anywhere, no providers."""
    return {
        "nodes": [
            {"id": "input", "type": "input", "label": "Topic", "config": {"name": "topic", "input_type": "text"}},
            {"id": "count", "type": "input", "label": "Count",
             "config": {"name": "count", "input_type": "number", "default": 3}},
            {"id": "text", "type": "text", "config": {"text": text}},
            {"id": "out", "type": "output", "label": "Result", "config": {"name": "result", "value": "{{text.text}}"}},
        ],
        "edges": [
            {"source": "input", "target": "text"},
            {"source": "count", "target": "text"},
            {"source": "text", "target": "out"},
        ],
    }


async def deploy(
    client, user, wid: str, *, expect: int | None = None, description: str | None = "Greets someone by name"
) -> dict[str, Any]:
    response = await client.post("/api/deployments", json={"workflow_id": wid, "description": description}, headers=user.headers)
    if expect is not None:
        assert response.status_code == expect, response.text
    return response.json()


async def rotate(client, user, deployment_id: str):
    return await client.post(f"/api/deployments/{deployment_id}/rotate-key", headers=user.headers)


async def deployed(client, user, graph: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    wid = await create_workflow(client, user, graph or greeting_graph(), name="Deployed")
    return wid, await deploy(client, user, wid, expect=201)


def key_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def call(client, deployment, key: str | None, inputs: dict[str, Any] | None = None, query: str = "", headers=None):
    headers = headers if headers is not None else (key_headers(key) if key else {})
    return await client.post(f"{deployment['endpoint']}{query}", json={"inputs": inputs or {}}, headers=headers)


def run_in_process(execution_id, session_factory, redis):
    return run_execution(execution_id, session_factory=session_factory, redis=redis, worker_id="worker@test")


@dataclass
class InlineWorkerQueue:
    """A task queue whose "worker" starts the run in the background as soon as it's queued."""

    session_factory: Any
    redis: Any
    enqueued: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    tasks: list[asyncio.Task] = field(default_factory=list)

    async def enqueue(self, execution_id: uuid.UUID, queue: str, *, segment: int = 0) -> str:
        self.enqueued.append((execution_id, queue))
        self.tasks.append(asyncio.create_task(run_in_process(execution_id, self.session_factory, self.redis)))
        return str(execution_id)

    async def revoke(self, task_id: str) -> None:
        pass

    async def finish(self) -> None:
        await asyncio.gather(*self.tasks)


@pytest.fixture
def inline_worker(client, session_factory, redis):
    worker = InlineWorkerQueue(session_factory, redis)
    app.dependency_overrides[get_task_queue] = lambda: worker
    return worker


# --- deploying ------------------------------------------------------------------------------


async def test_deploy_returns_the_key_once_and_stores_only_its_hash(client, user, shared_session, caplog):
    caplog.set_level(logging.DEBUG)
    wid, created = await deployed(client, user)

    key = created["api_key"]
    assert key.startswith("ffk_") and len(key) >= 40
    assert created["api_key_prefix"] == key[:12]
    assert created["endpoint"] == f"/api/v1/deployments/{created['id']}/run"
    assert created["workflow_id"] == wid and created["name"] == "Deployed"
    assert created["version"] == 1 and created["workflow_version"] >= 1
    # Input and Output nodes in execution order; `required` means "no default".
    assert created["inputs"] == [
        {"node_id": "input", "label": "Topic", "name": "topic", "type": "text", "required": True, "default": None},
        {"node_id": "count", "label": "Count", "name": "count", "type": "number", "required": False, "default": 3},
    ]
    assert created["outputs"] == [{"node_id": "out", "label": "Result", "name": "result"}]

    # Stored: the SHA-256 of the key and its display prefix. The key itself is nowhere.
    row = await shared_session.scalar(select(Deployment).where(Deployment.id == uuid.UUID(created["id"])))
    assert row.api_key_hash == hashlib.sha256(key.encode()).hexdigest()
    assert row.api_key_prefix == key[:12]
    assert key not in repr({column.key: getattr(row, column.key) for column in Deployment.__table__.columns})
    assert key not in caplog.text

    # Listing never shows it again.
    listed = (await client.get("/api/deployments", headers=user.headers)).json()
    assert [d["id"] for d in listed] == [created["id"]]
    assert "api_key" not in listed[0] and listed[0]["api_key_prefix"] == key[:12]


async def test_the_key_response_is_not_cacheable(client, user):
    wid = await create_workflow(client, user, greeting_graph())
    response = await client.post("/api/deployments", json={"workflow_id": wid, "description": "Greets"}, headers=user.headers)
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"


async def test_the_first_deploy_needs_a_description_and_redeploys_keep_it(client, user):
    wid = await create_workflow(client, user, greeting_graph())
    missing = await client.post("/api/deployments", json={"workflow_id": wid}, headers=user.headers)
    assert missing.status_code == 422 and "Describe what the pipeline does" in missing.json()["detail"]
    blank = await client.post("/api/deployments", json={"workflow_id": wid, "description": "   "}, headers=user.headers)
    assert blank.status_code == 422
    created = await deploy(client, user, wid, expect=201, description="  Greets someone  ")
    assert created["description"] == "Greets someone" and created["side_effects"] is False
    again = await deploy(client, user, wid, expect=200, description=None)
    assert again["description"] == "Greets someone"
    changed = await deploy(client, user, wid, expect=200, description="Says hello")
    assert changed["description"] == "Says hello"


async def test_redeploy_keeps_id_and_key_and_publishes_the_new_graph(client, user, task_queue, shared_session):
    wid, first = await deployed(client, user)
    key = first["api_key"]
    graph = greeting_graph("Hi {{input.subject}}")
    graph["nodes"][0]["config"]["name"] = "subject"
    await client.put(f"/api/workflows/{wid}", json={"graph": graph}, headers=user.headers)

    # Until redeployed, runs use the snapshot: the input is still called "topic".
    response = await call(client, first, key, {"topic": "a"})
    assert response.status_code == 202
    execution_id = uuid.UUID(response.json()["execution_id"])
    execution = (await client.get(f"/api/executions/{execution_id}", headers=user.headers)).json()
    assert execution["inputs"] == {"topic": "a"}
    queued = await shared_session.scalar(select(WorkflowExecution.graph_json).where(WorkflowExecution.id == execution_id))
    assert {n["id"]: n for n in queued["nodes"]}["text"]["config"]["text"] == "Hello {{input.topic}}"

    second = await deploy(client, user, wid, expect=200)
    assert second["id"] == first["id"] and second["endpoint"] == first["endpoint"]
    assert second["api_key"] is None and second["api_key_prefix"] == first["api_key_prefix"]
    assert second["version"] == 2 and second["workflow_version"] > first["workflow_version"]
    assert [i["name"] for i in second["inputs"]] == ["subject", "count"]
    assert (await call(client, second, key, {"subject": "b"})).status_code == 202  # same key still works


async def test_rotating_the_key_revokes_the_old_one_and_publishes_nothing(client, user, task_queue):
    wid, first = await deployed(client, user)
    # An unfinished edit must not go live just because the key was replaced.
    graph = greeting_graph("Draft {{input.topic}}")
    await client.put(f"/api/workflows/{wid}", json={"graph": graph}, headers=user.headers)

    response = await rotate(client, user, first["id"])
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    rotated = response.json()
    new_key = rotated["api_key"]
    assert new_key and new_key != first["api_key"] and rotated["api_key_prefix"] == new_key[:12]
    assert rotated["key_created_at"] > first["key_created_at"]
    assert rotated["version"] == first["version"] and rotated["workflow_version"] == first["workflow_version"]
    assert rotated["deployed_at"] == first["deployed_at"]

    assert (await call(client, rotated, first["api_key"], {"topic": "x"})).status_code == 401
    assert (await call(client, rotated, new_key, {"topic": "x"})).status_code == 202
    listed = (await client.get("/api/deployments", headers=user.headers)).json()
    assert "api_key" not in listed[0] and listed[0]["api_key_prefix"] == new_key[:12]


# --- undeploying ------------------------------------------------------------------------------


async def undeploy(client, user, deployment_id: str):
    return await client.delete(f"/api/deployments/{deployment_id}", headers=user.headers)


async def test_undeploy_revokes_the_endpoint_but_keeps_the_row(client, user, task_queue, shared_session):
    wid, created = await deployed(client, user)
    execution_id = (await call(client, created, created["api_key"], {"topic": "x"})).json()["execution_id"]

    response = await undeploy(client, user, created["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == created["id"] and body["revoked_at"] is not None and "api_key" not in body

    # The key that worked a moment ago now gets 404, on both the run and the status endpoint.
    gone = await call(client, created, created["api_key"], {"topic": "x"})
    assert gone.status_code == 404 and gone.json()["detail"] == "This deployment was undeployed"
    status_path = f"/api/v1/deployments/{created['id']}/executions/{execution_id}"
    assert (await client.get(status_path, headers=key_headers(created["api_key"]))).status_code == 404
    # Without the key it's the same 401 as any unknown deployment: nothing to probe.
    assert (await call(client, created, "ffk_wrong", {"topic": "x"})).status_code == 401

    # The row and its run stay for history; the default listing leaves it out.
    row = await shared_session.get(Deployment, uuid.UUID(created["id"]), populate_existing=True)
    assert row is not None and row.revoked_at is not None
    assert (await client.get("/api/deployments", headers=user.headers)).json() == []
    history = (await client.get(f"/api/deployments?workflow_id={wid}&include_revoked=true", headers=user.headers)).json()
    assert [d["id"] for d in history] == [created["id"]] and history[0]["revoked_at"] == body["revoked_at"]
    [execution] = (await client.get("/api/executions", headers=user.headers)).json()
    assert execution["id"] == execution_id and execution["deployment_id"] == created["id"]
    # The Triggers panel no longer offers it as the webhook.
    triggers = (await client.get(f"/api/workflows/{wid}/triggers", headers=user.headers)).json()
    webhook = next(t for t in triggers["triggers"] if t["type"] == "webhook")
    assert webhook["webhook"] is None
    assert any(w.startswith("Deploy the workflow") for w in webhook["warnings"])

    # Undeploying again changes nothing; rotating a revoked deployment's key is refused.
    again = await undeploy(client, user, created["id"])
    assert again.status_code == 200 and again.json()["revoked_at"] == body["revoked_at"]
    assert (await rotate(client, user, created["id"])).status_code == 409


async def test_deploying_again_after_undeploy_issues_a_new_key(client, user, task_queue):
    wid, created = await deployed(client, user)
    await undeploy(client, user, created["id"])
    response = await client.post("/api/deployments", json={"workflow_id": wid}, headers=user.headers)
    assert response.status_code == 200, response.text
    redeployed = response.json()
    assert redeployed["id"] == created["id"] and redeployed["revoked_at"] is None
    assert redeployed["api_key"] and redeployed["api_key"] != created["api_key"]
    assert redeployed["version"] == created["version"] + 1
    # The revoked key never comes back; the new one works.
    assert (await call(client, redeployed, created["api_key"], {"topic": "x"})).status_code == 401
    assert (await call(client, redeployed, redeployed["api_key"], {"topic": "x"})).status_code == 202


async def test_undeploy_is_owner_only(client, user_factory):
    owner, intruder = await user_factory(), await user_factory()
    _, created = await deployed(client, owner)
    assert (await undeploy(client, intruder, created["id"])).status_code == 404
    assert (await client.delete(f"/api/deployments/{created['id']}")).status_code == 401
    assert (await undeploy(client, owner, str(uuid.uuid4()))).status_code == 404
    assert (await call(client, created, created["api_key"], {"topic": "x"})).status_code == 202


async def test_an_invalid_graph_is_not_deployed(client, user):
    wid = await create_workflow(client, user, {"nodes": [{"id": "x", "type": "nope"}]})
    response = await client.post("/api/deployments", json={"workflow_id": wid}, headers=user.headers)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["message"] == "Workflow graph is invalid"
    assert detail["errors"][0]["node_id"] == "x"
    assert (await client.get("/api/deployments", headers=user.headers)).json() == []


async def test_deleting_the_workflow_removes_the_deployment(client, user):
    wid, created = await deployed(client, user)
    assert (await client.delete(f"/api/workflows/{wid}", headers=user.headers)).status_code == 204
    assert (await client.get("/api/deployments", headers=user.headers)).json() == []
    assert (await call(client, created, created["api_key"], {"topic": "x"})).status_code == 401


# --- ownership --------------------------------------------------------------------------------


async def test_deployments_are_owner_only(client, user_factory):
    owner, intruder = await user_factory(), await user_factory()
    wid, created = await deployed(client, owner)

    assert (await client.post("/api/deployments", json={"workflow_id": wid})).status_code == 401
    assert (await client.post("/api/deployments", json={"workflow_id": wid}, headers=intruder.headers)).status_code == 404
    assert (await rotate(client, intruder, created["id"])).status_code == 404
    assert (await client.post(f"/api/deployments/{created['id']}/rotate-key")).status_code == 401
    assert (await rotate(client, owner, str(uuid.uuid4()))).status_code == 404
    assert (await client.get("/api/deployments", headers=intruder.headers)).json() == []
    assert (await client.get(f"/api/deployments?workflow_id={wid}", headers=intruder.headers)).json() == []
    # The intruder's attempt changed nothing: the owner's key still works.
    assert (await call(client, created, created["api_key"], {"topic": "x"})).status_code == 202

    # The owner sees API runs with the rest of their executions; the intruder doesn't.
    [execution] = (await client.get("/api/executions", headers=owner.headers)).json()
    assert execution["trigger"] == "webhook" and execution["deployment_id"] == created["id"]
    assert execution["triggered_by_user_id"] is None
    assert (await client.get("/api/executions", headers=intruder.headers)).json() == []
    assert (await client.get(f"/api/executions/{execution['id']}", headers=intruder.headers)).status_code == 404


async def test_a_key_only_opens_its_own_deployment(client, user_factory, task_queue):
    alice, bob = await user_factory(), await user_factory()
    _, a = await deployed(client, alice)
    _, b = await deployed(client, bob)

    assert (await call(client, b, a["api_key"], {"topic": "x"})).status_code == 401
    run_b = await call(client, b, b["api_key"], {"topic": "x"})
    assert run_b.status_code == 202
    # A's key can't read B's run: not on B's path (wrong key), nor on A's (not A's run).
    b_run = run_b.json()["execution_id"]
    on_b = await client.get(f"/api/v1/deployments/{b['id']}/executions/{b_run}", headers=key_headers(a["api_key"]))
    on_a = await client.get(f"/api/v1/deployments/{a['id']}/executions/{b_run}", headers=key_headers(a["api_key"]))
    assert (on_b.status_code, on_a.status_code) == (401, 404)


async def test_a_manual_run_is_not_visible_through_the_deployment(client, user, task_queue):
    wid, created = await deployed(client, user)
    manual = await queue_run(client, user, wid, {"topic": "x"})
    response = await client.get(
        f"/api/v1/deployments/{created['id']}/executions/{manual}", headers=key_headers(created["api_key"])
    )
    assert response.status_code == 404


# --- authentication ---------------------------------------------------------------------------


async def test_authentication_failures_are_401(client, user, task_queue):
    _, created = await deployed(client, user)
    key = created["api_key"]
    unknown = {**created, "endpoint": f"/api/v1/deployments/{uuid.uuid4()}/run"}

    cases = {
        "no key": await call(client, created, None, {"topic": "x"}),
        "wrong key": await call(client, created, "ffk_" + "x" * 43, {"topic": "x"}),
        "key off by one char": await call(client, created, key[:-1] + ("A" if key[-1] != "A" else "B"), {"topic": "x"}),
        "a user's JWT": await call(client, created, None, {"topic": "x"}, headers=user.headers),
        "basic auth": await call(client, created, None, {"topic": "x"}, headers={"Authorization": f"Basic {key}"}),
        "unknown deployment": await call(client, unknown, key, {"topic": "x"}),
    }
    for case, response in cases.items():
        assert response.status_code == 401, case
        assert response.headers["www-authenticate"] == "Bearer", case
    # An unknown deployment looks exactly like a wrong key.
    assert cases["unknown deployment"].json() == cases["wrong key"].json() == {"detail": "Invalid deployment or API key"}
    assert "Missing API key" in cases["no key"].json()["detail"]
    assert task_queue.enqueued == []

    # Both header forms work.
    assert (await call(client, created, None, {"topic": "x"}, headers={"X-API-Key": key})).status_code == 202
    assert (await call(client, created, key, {"topic": "x"})).status_code == 202


async def test_status_endpoint_needs_the_key(client, user, task_queue):
    _, created = await deployed(client, user)
    execution_id = (await call(client, created, created["api_key"], {"topic": "x"})).json()["execution_id"]
    path = f"/api/v1/deployments/{created['id']}/executions/{execution_id}"
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=key_headers("ffk_wrong"))).status_code == 401
    assert (await client.get(path, headers=user.headers)).status_code == 401


# --- running ----------------------------------------------------------------------------------


async def test_async_run_returns_202_and_the_status_link_follows_it(client, user, task_queue, session_factory, redis):
    _, created = await deployed(client, user)
    response = await call(client, created, created["api_key"], {"topic": "world"})

    assert response.status_code == 202
    body = response.json()
    execution_id = uuid.UUID(body["execution_id"])
    assert body["deployment_id"] == created["id"] and body["status"] == "pending"
    assert body["final_output"] is None and body["error"] is None
    assert body["links"] == {"status": f"/api/v1/deployments/{created['id']}/executions/{execution_id}"}
    # Queued exactly like an editor run (all nodes are portable: the default queue).
    assert task_queue.enqueued == [(execution_id, "default")]

    headers = key_headers(created["api_key"])
    assert (await client.get(body["links"]["status"], headers=headers)).json()["status"] == "pending"
    summary = await run_in_process(execution_id, session_factory, redis)
    assert summary["status"] == "success"

    done = (await client.get(body["links"]["status"], headers=headers)).json()
    assert done["status"] == "success"
    assert done["final_output"] == {"result": "Hello world"}
    assert done["started_at"] and done["finished_at"] and done["duration_ms"] is not None
    # No per-node internals leak through the public API.
    assert "node_executions" not in done and "inputs" not in done


async def test_wait_returns_the_final_output(client, user, inline_worker):
    _, created = await deployed(client, user)
    response = await call(client, created, created["api_key"], {"topic": "world", "count": 5}, query="?wait=true&timeout=20")
    await inline_worker.finish()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success" and body["error"] is None
    assert body["final_output"] == {"result": "Hello world"}
    assert inline_worker.enqueued == [(uuid.UUID(body["execution_id"]), "default")]


async def test_wait_reports_a_failed_run_with_its_error(client, user, inline_worker):
    _, created = await deployed(client, user)
    response = await call(client, created, created["api_key"], {}, query="?wait=true&timeout=20")  # no topic
    await inline_worker.finish()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed" and body["final_output"] is None
    assert "Missing required input 'topic'" in body["error"]


async def test_wait_answers_202_when_the_run_outlasts_the_timeout(client, user, task_queue):
    _, created = await deployed(client, user, delay_graph(30))
    loop = asyncio.get_running_loop()
    started = loop.time()
    response = await call(client, created, created["api_key"], query="?wait=true&timeout=0.3")
    elapsed = loop.time() - started

    assert response.status_code == 202
    assert response.json()["status"] == "pending" and response.json()["links"]["status"]
    assert 0.3 <= elapsed < 3


async def test_wait_falls_back_to_polling_without_redis_events(client, user, inline_worker, monkeypatch):
    from contextlib import asynccontextmanager

    from app.api.routes import deployment_runs

    @asynccontextmanager
    async def no_events(redis, execution_id):
        yield None

    monkeypatch.setattr(deployment_runs, "execution_events", no_events)
    monkeypatch.setattr(deployment_service, "WAIT_DB_CHECK_SECONDS", 0.05)
    _, created = await deployed(client, user)
    response = await call(client, created, created["api_key"], {"topic": "poll"}, query="?wait=true&timeout=20")
    await inline_worker.finish()
    assert response.status_code == 200 and response.json()["final_output"] == {"result": "Hello poll"}


async def test_wait_timeout_is_bounded(client, user, task_queue):
    _, created = await deployed(client, user)
    too_long = settings.DEPLOYMENT_MAX_WAIT_SECONDS + 1
    response = await call(client, created, created["api_key"], {"topic": "x"}, query=f"?wait=true&timeout={too_long}")
    assert response.status_code == 422
    assert task_queue.enqueued == []


async def test_bad_inputs_are_422_before_anything_is_queued(client, user, user_factory, task_queue):
    graph = {
        "nodes": [
            {"id": "doc", "type": "input", "config": {"name": "document", "input_type": "file"}},
            {"id": "out", "type": "output", "config": {"value": "{{doc.document}}"}},
        ],
        "edges": [{"source": "doc", "target": "out"}],
    }
    _, created = await deployed(client, user, graph)
    response = await client.post(created["endpoint"], json={"inputs": "not an object"}, headers=key_headers(created["api_key"]))
    assert response.status_code == 422
    # A file id that isn't the owner's upload is refused, as for an editor run.
    response = await call(client, created, created["api_key"], {"document": str(uuid.uuid4())})
    assert response.status_code == 422
    assert response.json()["detail"]["errors"][0]["code"] == "invalid_input"
    assert task_queue.enqueued == []


async def test_a_deployment_that_no_longer_validates_is_422(client, user, task_queue, monkeypatch):
    graph = {
        "nodes": [
            {"id": "llm", "type": "gemini", "config": {"user_prompt": "Hi"}},
            {"id": "out", "type": "output", "config": {"value": "{{llm.response}}"}},
        ],
        "edges": [{"source": "llm", "target": "out"}],
    }
    _, created = await deployed(client, user, graph)
    monkeypatch.setattr(settings, "TESTING", False)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)

    response = await call(client, created, created["api_key"])
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "redeploy" in detail["message"] and detail["errors"][0]["code"] == "auth_missing"
    assert task_queue.enqueued == []


async def test_broker_down_is_503_and_the_execution_is_failed(client, user, task_queue):
    _, created = await deployed(client, user)
    task_queue.fail = True
    response = await call(client, created, created["api_key"], {"topic": "x"}, query="?wait=true")
    assert response.status_code == 503
    [execution] = (await client.get("/api/executions", headers=user.headers)).json()
    assert execution["status"] == "failed" and "Could not queue the run" in execution["error_message"]


# --- rate limiting ------------------------------------------------------------------------------


@pytest.fixture
def rate_limited(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", True)
    monkeypatch.setattr(settings, "DEPLOYMENT_RUN_RATE_LIMIT", "2/minute")
    monkeypatch.setattr(settings, "DEPLOYMENT_STATUS_RATE_LIMIT", "3/minute")
    limiter.reset()
    yield
    limiter.reset()


async def test_runs_are_rate_limited_per_deployment(client, user, task_queue, rate_limited):
    _, a = await deployed(client, user)
    _, b = await deployed(client, user)

    statuses = [(await call(client, a, a["api_key"], {"topic": "x"})).status_code for _ in range(3)]
    assert statuses == [202, 202, 429]
    limited = await call(client, a, a["api_key"], {"topic": "x"})
    assert limited.status_code == 429 and "Too many requests" in limited.json()["detail"]
    assert len(task_queue.enqueued) == 2
    # Another deployment has its own budget.
    assert (await call(client, b, b["api_key"], {"topic": "x"})).status_code == 202
    # Requests without a valid key are refused before they count.
    assert (await call(client, b, "ffk_wrong", {"topic": "x"})).status_code == 401
    assert (await call(client, b, b["api_key"], {"topic": "x"})).status_code == 202


async def test_status_polling_has_its_own_limit(client, user, task_queue, rate_limited):
    _, created = await deployed(client, user)
    link = (await call(client, created, created["api_key"], {"topic": "x"})).json()["links"]["status"]
    headers = key_headers(created["api_key"])
    statuses = [(await client.get(link, headers=headers)).status_code for _ in range(4)]
    assert statuses == [200, 200, 200, 429]
