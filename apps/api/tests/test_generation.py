"""Generate with AI: the catalog, checking and retrying a draft (unknown node types, broken
edges, invalid config, bad references, non-JSON), giving up cleanly, and the endpoint."""

import json

import pytest
from flowforge_engine import ExecutionServices, ProviderSettings

from app.api.routes.workflows import get_generator_llm
from app.main import app
from app.services.generation import MAX_ATTEMPTS, GenerationFailed, generate, node_catalog, normalize

GOOD = {
    "name": "Topic to summary", "description": "Summarizes a topic.",
    "nodes": [
        {"id": "topic", "type": "input", "config": {"name": "topic", "default": "tides"}},
        {"id": "llm", "type": "gemini", "config": {"provider": "mock", "user_prompt": "Explain {{topic.value}}"}},
        {"id": "out", "type": "output", "config": {"value": "{{llm.response}}"}},
    ],
    "edges": [{"source": "topic", "target": "llm"}, {"source": "llm", "target": "out"}],
}


def scripted(*replies):
    queue, prompts = list(replies), []

    async def call(system, prompt):
        prompts.append(prompt)
        reply = queue.pop(0)
        return reply if isinstance(reply, str) else json.dumps(reply)

    call.prompts = prompts  # type: ignore[attr-defined]
    return call


def services():
    return ExecutionServices(provider_settings=ProviderSettings(testing=True))


def with_node(graph, **changes):
    copy = json.loads(json.dumps(graph))
    copy["nodes"][1].update(changes)
    return copy


def test_catalog_lists_every_node_with_fields_outputs_and_item_fields():
    catalog = node_catalog()
    assert catalog.startswith("- input:")
    assert "- gmail: " in catalog and "subject: string (required)" in catalog
    assert "- condition: " in catalog and "Branches: true, false" in catalog
    assert "each item of emails[]: from, from_address" in catalog and "body_text" in catalog
    assert "privacy_guard" not in catalog


async def test_a_good_draft_is_accepted_on_the_first_try_and_laid_out():
    result = await generate(scripted(GOOD), "explain a topic", services())
    assert result.attempts == 1 and result.name == "Topic to summary"
    positions = [n["position"]["x"] for n in result.graph["nodes"]]
    assert positions == [0, 320, 640]  # one column per step


async def test_an_unknown_node_type_is_sent_back_and_retried():
    llm = scripted(with_node(GOOD, type="chatgpt"), GOOD)
    result = await generate(llm, "explain", services())
    assert result.attempts == 2
    assert "uses type 'chatgpt', which doesn't exist" in llm.prompts[1]


async def test_validation_problems_are_sent_back_with_the_draft():
    broken = with_node(GOOD, config={"provider": "mock", "user_prompt": "Explain {{nowhere.value}}"})
    llm = scripted(broken, GOOD)
    result = await generate(llm, "explain", services())
    assert result.attempts == 2
    assert "nowhere" in llm.prompts[1] and "Previous answer:" in llm.prompts[1]


async def test_non_json_and_bad_edges_are_retried():
    edges = json.loads(json.dumps(GOOD))
    edges["edges"].append({"source": "llm", "target": "ghost"})
    llm = scripted("Sure! Here's a pipeline...", edges, GOOD)
    result = await generate(llm, "explain", services())
    assert result.attempts == 3
    assert "wasn't JSON" in llm.prompts[1] and "weren't declared" in llm.prompts[2]


async def test_gives_up_after_the_last_attempt_with_the_problems():
    bad = with_node(GOOD, type="nope")
    with pytest.raises(GenerationFailed) as failure:
        await generate(scripted(*[bad] * MAX_ATTEMPTS), "explain", services())
    assert failure.value.attempts == MAX_ATTEMPTS and "doesn't exist" in failure.value.problems[0]


def test_file_inputs_never_keep_an_invented_default():
    graph, problems = normalize({"nodes": [{"id": "doc", "type": "input",
                                            "config": {"name": "doc", "input_type": "file", "default": "sample.pdf"}}],
                                 "edges": []})
    assert problems == [] and "default" not in graph["nodes"][0]["config"]


@pytest.fixture
def generator():
    def use(*replies):
        app.dependency_overrides[get_generator_llm] = lambda: scripted(*replies)
    yield use
    app.dependency_overrides.pop(get_generator_llm, None)


async def test_the_endpoint_saves_a_valid_pipeline(client, user, generator):
    generator(GOOD)
    response = await client.post("/api/workflows/generate", json={"prompt": "explain a topic"}, headers=user.headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["attempts"] == 1 and body["workflow"]["name"] == "Topic to summary"
    saved = (await client.get(f"/api/workflows/{body['workflow']['id']}", headers=user.headers)).json()
    assert [n["id"] for n in saved["graph"]["nodes"]] == ["topic", "llm", "out"]
    run = (await client.post(f"/api/workflows/{saved['id']}/run?sync=true", json={"inputs": {}}, headers=user.headers)).json()
    assert run["status"] == "success"


async def test_the_endpoint_saves_nothing_when_no_draft_validates(client, user, generator):
    generator(*[with_node(GOOD, type="nope")] * MAX_ATTEMPTS)
    before = len((await client.get("/api/workflows", headers=user.headers)).json())
    response = await client.post("/api/workflows/generate", json={"prompt": "explain a topic"}, headers=user.headers)
    assert response.status_code == 422
    assert "doesn't exist" in response.json()["detail"]["problems"][0]
    assert len((await client.get("/api/workflows", headers=user.headers)).json()) == before
