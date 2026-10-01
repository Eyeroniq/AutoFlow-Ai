"""Endpoints the visual editor relies on: node catalog, unsaved-graph validation, duplicate,
single-node tests, and the dashboard/history listings."""

import copy

from flowforge_engine import ExecutionServices, ProviderSettings, WorkflowGraph, validate_workflow
from flowforge_engine.providers import MockEmailProvider

from app.db.seed import demo_graph
from app.schemas.workflow import EXAMPLE_GRAPH
from tests.support import create_workflow, queue_run


# --- GET /api/nodes -----------------------------------------------------------------------


async def test_node_catalog_comes_from_the_registry(client, user):
    response = await client.get("/api/nodes", headers=user.headers)
    assert response.status_code == 200
    nodes = {n["type"]: n for n in response.json()}
    assert {"input", "output", "text", "condition", "delay", "gemini", "groq", "openrouter", "ollama",
            "openai", "anthropic", "gmail", "gmail_read", "http_request"} <= set(nodes)

    gemini = nodes["gemini"]
    assert gemini["group"] == "LLM" and gemini["category"] == "ai" and gemini["icon"] == "sparkles"
    assert gemini["queue"] == "llm" and gemini["portable"] is False and gemini["interruptible"] is True
    assert nodes["input"]["portable"] is True and nodes["http_request"]["queue"] == "default"
    assert nodes["ocr"]["queue"] == "ocr" and nodes["ocr"]["group"] == "Documents"
    assert {"pdf_extract", "ocr", "summarize", "extract_entities"} <= set(nodes)
    props = gemini["config_schema"]["properties"]
    assert {"provider", "model", "system_prompt", "user_prompt", "temperature", "max_tokens", "fallback", "stream"} <= set(props)
    assert gemini["config_schema"]["required"] == ["user_prompt"]
    assert {"response", "provider_used", "model"} <= set(gemini["output_keys"])

    assert nodes["input"]["group"] == "General" and nodes["input"]["has_input"] is False
    assert nodes["input"]["output_keys"] is None  # depends on the configured name
    assert nodes["condition"]["branches"] == ["true", "false"]
    assert nodes["gmail"]["group"] == "Integrations" and nodes["gmail"]["interruptible"] is False
    assert {"message_id", "from"} <= set(nodes["gmail"]["output_keys"])
    assert nodes["output"]["produces_final_output"] is True

    groups = [n["group"] for n in response.json()]
    order = ["General", "LLM", "Lists", "Data sources", "Integrations", "Documents", "Knowledge", "Audio", "Privacy"]
    assert groups == sorted(groups, key=order.index)  # grouped, in order

    assert (await client.get("/api/nodes")).status_code == 401


# --- validate an unsaved graph --------------------------------------------------------------


async def test_validate_accepts_an_unsaved_graph(client, user):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    broken = copy.deepcopy(EXAMPLE_GRAPH)
    broken["edges"].append({"source": "output", "target": "input"})  # cycle
    del broken["nodes"][1]["config"]["user_prompt"]  # missing required field
    broken["nodes"][2]["config"]["body"] = "{{gemini.respnse}}"  # typo'd reference

    result = (await client.post(f"/api/workflows/{wid}/validate", json={"graph": broken}, headers=user.headers)).json()
    assert result["valid"] is False
    by_code = {}
    for issue in result["errors"]:
        by_code.setdefault(issue["code"], []).append(issue)
    assert by_code["missing_config"][0]["node_id"] == "gemini" and by_code["missing_config"][0]["field"] == "user_prompt"
    assert by_code["cycle"][0]["message"].startswith("Workflow contains a cycle")
    assert any("has no output 'respnse'" in i["message"] for i in by_code["unresolvable_reference"])

    # The saved graph is untouched and still valid.
    saved = (await client.post(f"/api/workflows/{wid}/validate", headers=user.headers)).json()
    assert saved == {"valid": True, "errors": []}


# --- duplicate ----------------------------------------------------------------------------------


async def test_duplicate_copies_the_graph(client, user, user_factory):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH, name="Original")
    response = await client.post(f"/api/workflows/{wid}/duplicate", headers=user.headers)
    assert response.status_code == 201
    dup = response.json()
    assert dup["id"] != wid and dup["name"] == "Original (copy)" and dup["version"] == 1
    original = (await client.get(f"/api/workflows/{wid}", headers=user.headers)).json()
    assert dup["graph"] == original["graph"]
    # It has its own node rows, so it can run.
    assert (await client.post(f"/api/workflows/{dup['id']}/validate", headers=user.headers)).json()["valid"]

    stranger = await user_factory()
    assert (await client.post(f"/api/workflows/{wid}/duplicate", headers=stranger.headers)).status_code == 404


# --- single-node test ----------------------------------------------------------------------------


async def test_node_test_resolves_sample_upstream_data(client, user):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    response = await client.post(
        f"/api/workflows/{wid}/nodes/gemini/test",
        json={"upstream_outputs": {"input": {"value": "tides", "topic": "tides"}}},
        headers=user.headers,
    )
    assert response.status_code == 200
    result = response.json()
    assert result["node_key"] == "gemini" and result["status"] == "success" and result["error"] is None
    assert result["input"]["user_prompt"] == "Write a three-sentence summary of tides."
    assert result["output"]["response"] == "[MOCK RESPONSE to: Write a three-sentence summary of tides.]"
    assert result["duration_ms"] is not None and result["started_at"] and result["finished_at"]
    # Not recorded as an execution.
    assert (await client.get(f"/api/workflows/{wid}/executions", headers=user.headers)).json() == []


async def test_node_test_reports_errors_and_uses_unsaved_config(client, user):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    missing = (await client.post(f"/api/workflows/{wid}/nodes/gemini/test", json={}, headers=user.headers)).json()
    assert missing["status"] == "failed"
    assert missing["error"].startswith("Cannot resolve '{{input.topic}}'")

    override = (await client.post(
        f"/api/workflows/{wid}/nodes/gemini/test",
        json={"config": {"user_prompt": "Say hi to {{vars.recipient}}"}},
        headers=user.headers,
    )).json()
    assert override["status"] == "success"
    assert override["input"]["user_prompt"] == "Say hi to you@example.com"  # saved graph variable

    invalid = (await client.post(
        f"/api/workflows/{wid}/nodes/gemini/test", json={"config": {"user_prompt": "hi", "temperature": 9}},
        headers=user.headers,
    )).json()
    assert invalid["status"] == "failed" and "temperature" in invalid["error"]


async def test_node_test_gmail_sends_through_the_configured_provider(client, user):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    result = (await client.post(
        f"/api/workflows/{wid}/nodes/gmail/test",
        json={"upstream_outputs": {"input": {"topic": "x", "value": "x"}, "gemini": {"response": "Hello"}},
              "variables": {"recipient": "someone@example.com"}},
        headers=user.headers,
    )).json()
    assert result["status"] == "success" and result["output"]["to"] == ["someone@example.com"]
    [sent] = MockEmailProvider.outbox()
    assert sent.body == "Hello"


async def test_node_test_ownership_and_unknown_nodes(client, user, user_factory):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH)
    assert (await client.post(f"/api/workflows/{wid}/nodes/nope/test", headers=user.headers)).status_code == 404
    stranger = await user_factory()
    assert (await client.post(f"/api/workflows/{wid}/nodes/gemini/test", headers=stranger.headers)).status_code == 404


# --- listings -----------------------------------------------------------------------------------


async def test_workflow_list_shows_size_and_last_run(client, user):
    wid = await create_workflow(client, user, EXAMPLE_GRAPH, name="With runs")
    empty = await create_workflow(client, user, {"nodes": []}, name="Empty")
    first = await queue_run(client, user, wid, {"topic": "a"})
    second = await queue_run(client, user, wid, {"topic": "b"})

    listing = {w["id"]: w for w in (await client.get("/api/workflows", headers=user.headers)).json()}
    assert listing[wid]["node_count"] == 4 and listing[empty]["node_count"] == 0
    assert listing[wid]["last_execution"]["id"] in {str(first), str(second)}
    assert listing[wid]["last_execution"]["status"] == "pending"
    assert listing[empty]["last_execution"] is None


async def test_execution_history_across_workflows(client, user, user_factory):
    a = await create_workflow(client, user, EXAMPLE_GRAPH, name="Alpha")
    b = await create_workflow(client, user, EXAMPLE_GRAPH, name="Beta")
    ea = await queue_run(client, user, a, {"topic": "x"})
    eb = await queue_run(client, user, b, {"topic": "y"})
    await client.post(f"/api/executions/{ea}/stop", headers=user.headers)

    everything = (await client.get("/api/executions", headers=user.headers)).json()
    assert {(e["id"], e["workflow_name"]) for e in everything} == {(str(eb), "Beta"), (str(ea), "Alpha")}
    # Newest first. (Both rows share one test transaction, so their created_at can tie.)
    # Queued runs have no started_at yet; created_at says when they were requested.
    created = [e["created_at"] for e in everything]
    assert all(created) and created == sorted(created, reverse=True)
    stopped = (await client.get("/api/executions?status=stopped", headers=user.headers)).json()
    assert [e["id"] for e in stopped] == [str(ea)]
    only_b = (await client.get(f"/api/executions?workflow_id={b}", headers=user.headers)).json()
    assert [e["id"] for e in only_b] == [str(eb)]

    stranger = await user_factory()
    assert (await client.get("/api/executions", headers=stranger.headers)).json() == []


# --- seed ---------------------------------------------------------------------------------------


def test_seeded_demo_pipeline_is_valid():
    graph = WorkflowGraph.model_validate(demo_graph("me@example.com"))
    assert [n.id for n in graph.nodes] == ["input", "gemini", "gmail", "output"]
    assert validate_workflow(graph) == []
    services = ExecutionServices(provider_settings=ProviderSettings(testing=True))
    assert validate_workflow(graph, services=services) == []
    # Editor-only fields round-trip through the graph model.
    assert graph.model_dump(mode="json")["nodes"][0]["description"] == "What to write about"

    # With a Groq key on the server, Gemini falls back to Groq.
    with_fallback = WorkflowGraph.model_validate(demo_graph("me@example.com", ["groq"]))
    assert next(n for n in with_fallback.nodes if n.id == "gemini").config["fallback"] == ["groq"]
    assert validate_workflow(with_fallback, services=services) == []
