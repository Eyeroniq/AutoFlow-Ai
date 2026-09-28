import copy
import uuid

from flowforge_engine.providers import MockEmailProvider

from app.schemas.workflow import EXAMPLE_GRAPH


async def workflow_with_graph(client, user, graph=EXAMPLE_GRAPH):
    wid = (await client.post("/api/workflows", json={"name": "Run me"}, headers=user.headers)).json()["id"]
    response = await client.put(f"/api/workflows/{wid}", json={"graph": graph}, headers=user.headers)
    assert response.status_code == 200, response.text
    return wid


async def test_input_gemini_gmail_output_end_to_end(client, user):
    """The Phase 2 acceptance flow: create → PUT graph → validate → run → history."""
    create = await client.post("/api/workflows", json={"name": "Summarize and email"}, headers=user.headers)
    wid = create.json()["id"]
    put = await client.put(f"/api/workflows/{wid}", json={"graph": EXAMPLE_GRAPH}, headers=user.headers)
    assert put.status_code == 200

    validation = await client.post(f"/api/workflows/{wid}/validate", headers=user.headers)
    assert validation.json() == {"valid": True, "errors": []}

    run = await client.post(
        f"/api/workflows/{wid}/run?sync=true", json={"inputs": {"topic": "solar power"}}, headers=user.headers
    )
    assert run.status_code == 200, run.text
    execution = run.json()

    assert execution["status"] == "success"
    assert execution["trigger"] == "manual"
    assert execution["triggered_by_user_id"] == user.id
    assert execution["error_message"] is None
    assert execution["duration_ms"] >= 0

    nodes = execution["node_executions"]
    assert [n["node_key"] for n in nodes] == ["input", "gemini", "gmail", "output"]
    assert {n["status"] for n in nodes} == {"success"}
    for n in nodes:
        assert n["node_id"] is not None
        assert n["duration_ms"] is not None and n["started_at"] and n["finished_at"]
    by_key = {n["node_key"]: n for n in nodes}

    expected_response = "[MOCK RESPONSE to: Write a three-sentence summary of solar power.]"
    gemini = by_key["gemini"]
    assert gemini["node_type"] == "gemini" and gemini["node_label"] == "Summarize"
    assert gemini["input"]["user_prompt"] == "Write a three-sentence summary of solar power."
    assert gemini["output"] == {
        "response": expected_response, "provider": "gemini", "provider_used": "gemini",
        "model": "gemini-3.5-flash-lite", "mock": True, "fallback_errors": [],
    }

    gmail = by_key["gmail"]
    assert gmail["input"]["body"] == expected_response
    assert gmail["output"]["status"] == "sent"
    assert gmail["output"]["message_id"].startswith("mock-")
    assert gmail["output"]["to"] == ["you@example.com"]

    final = execution["final_output"]["result"]
    assert final["summary"] == expected_response
    assert final["email"]["status"] == "sent"
    assert final["email"]["message_id"] == gmail["output"]["message_id"]

    [sent] = MockEmailProvider.outbox()
    assert (sent.message_id, sent.subject, sent.body) == (
        gmail["output"]["message_id"], "Summary: solar power", expected_response,
    )

    listing = await client.get(f"/api/workflows/{wid}/executions", headers=user.headers)
    assert [(e["id"], e["status"]) for e in listing.json()] == [(execution["id"], "success")]

    detail = await client.get(f"/api/executions/{execution['id']}", headers=user.headers)
    assert detail.status_code == 200
    assert detail.json() == execution


async def test_run_without_body_uses_input_defaults(client, user):
    wid = await workflow_with_graph(client, user)
    run = (await client.post(f"/api/workflows/{wid}/run?sync=true", headers=user.headers)).json()
    assert run["status"] == "success"
    assert run["node_executions"][0]["output"]["topic"] == "the history of workflow automation"


async def test_invalid_graph_returns_422_and_records_nothing(client, user):
    graph = copy.deepcopy(EXAMPLE_GRAPH)
    graph["nodes"][1]["config"].pop("user_prompt")
    wid = await workflow_with_graph(client, user, graph)

    response = await client.post(f"/api/workflows/{wid}/run?sync=true", headers=user.headers)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["message"] == "Workflow graph is invalid"
    assert detail["errors"][0]["code"] == "missing_config"
    assert (await client.get(f"/api/workflows/{wid}/executions", headers=user.headers)).json() == []


async def test_failed_node_is_recorded_and_downstream_skipped(client, user):
    graph = copy.deepcopy(EXAMPLE_GRAPH)
    graph["variables"] = [{"key": "recipient", "value": "not-an-email"}]
    wid = await workflow_with_graph(client, user, graph)

    execution = (await client.post(f"/api/workflows/{wid}/run?sync=true", headers=user.headers)).json()

    assert execution["status"] == "failed"
    assert execution["final_output"] is None
    assert execution["error_message"] == "Node 'gmail' failed: Invalid email address(es): not-an-email"
    by_key = {n["node_key"]: n for n in execution["node_executions"]}
    assert [by_key[k]["status"] for k in ("input", "gemini", "gmail", "output")] == [
        "success", "success", "failed", "skipped",
    ]
    assert by_key["output"]["error_message"] == "Execution stopped after node 'gmail' failed"
    assert by_key["output"]["started_at"] is None
    assert execution["node_executions"][-1]["node_key"] == "output"  # skipped rows sort last
    assert MockEmailProvider.outbox() == []


async def test_condition_branch_not_taken_is_skipped_not_failed(client, user):
    graph = {
        "nodes": [
            {"id": "amount", "type": "input", "config": {"input_type": "number"}},
            {"id": "check", "type": "condition", "config": {"left": "{{amount.value}}", "operator": "greater_than", "right": 100}},
            {"id": "approve", "type": "text", "config": {"text": "approved {{amount.value}}"}},
            {"id": "reject", "type": "text", "config": {"text": "rejected"}},
        ],
        "edges": [
            {"source": "amount", "target": "check"},
            {"source": "check", "target": "approve", "source_handle": "true"},
            {"source": "check", "target": "reject", "source_handle": "false"},
        ],
    }
    wid = await workflow_with_graph(client, user, graph)
    execution = (await client.post(f"/api/workflows/{wid}/run?sync=true", json={"inputs": {"amount": "250"}}, headers=user.headers)).json()

    assert execution["status"] == "success"
    by_key = {n["node_key"]: n for n in execution["node_executions"]}
    assert by_key["approve"]["output"] == {"text": "approved 250"}
    assert by_key["reject"]["status"] == "skipped"
    assert by_key["reject"]["error_message"] == "Not reached: condition 'check' took the 'true' branch"
    assert execution["final_output"] == {"approve": {"text": "approved 250"}}


async def test_executions_are_listed_newest_first_with_paging(client, user):
    wid = await workflow_with_graph(client, user)
    ids = [(await client.post(f"/api/workflows/{wid}/run?sync=true", headers=user.headers)).json()["id"] for _ in range(3)]

    listing = (await client.get(f"/api/workflows/{wid}/executions", headers=user.headers)).json()
    assert [e["id"] for e in listing] == ids[::-1]
    page = (await client.get(f"/api/workflows/{wid}/executions?limit=1&offset=1", headers=user.headers)).json()
    assert [e["id"] for e in page] == [ids[1]]
    assert (await client.get(f"/api/workflows/{wid}/executions?limit=0", headers=user.headers)).status_code == 422


async def test_executions_are_private(client, user_factory):
    owner, other = await user_factory(), await user_factory()
    wid = await workflow_with_graph(client, owner)
    execution_id = (await client.post(f"/api/workflows/{wid}/run?sync=true", headers=owner.headers)).json()["id"]

    assert (await client.get(f"/api/executions/{execution_id}", headers=other.headers)).status_code == 404
    assert (await client.get(f"/api/executions/{uuid.uuid4()}", headers=owner.headers)).status_code == 404
    assert (await client.get(f"/api/executions/{execution_id}")).status_code == 401


async def test_deleting_a_workflow_deletes_its_executions(client, user):
    wid = await workflow_with_graph(client, user)
    execution_id = (await client.post(f"/api/workflows/{wid}/run?sync=true", headers=user.headers)).json()["id"]
    await client.delete(f"/api/workflows/{wid}", headers=user.headers)
    assert (await client.get(f"/api/executions/{execution_id}", headers=user.headers)).status_code == 404
