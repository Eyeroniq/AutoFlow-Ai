"""Provider resolution in the API: mocks only under TESTING, user keys over server keys,
and a missing key is an "Authentication missing" validation error — never a silent mock."""

import pytest
from flowforge_engine.providers import MockEmailProvider, MockLLMProvider
from flowforge_engine.providers.gemini_provider import GeminiProvider
from pydantic import SecretStr

from app.core.config import settings
from app.schemas.workflow import EXAMPLE_GRAPH
from app.services.credentials import provider_settings_for, server_provider_settings


async def create_workflow(client, user, graph=EXAMPLE_GRAPH):
    wid = (await client.post("/api/workflows", json={"name": "Providers"}, headers=user.headers)).json()["id"]
    response = await client.put(f"/api/workflows/{wid}", json={"graph": graph}, headers=user.headers)
    assert response.status_code == 200, response.text
    return wid


@pytest.fixture
def no_server_keys(monkeypatch):
    """A real (non-testing) server whose .env has no provider keys."""
    monkeypatch.setattr(settings, "TESTING", False)
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                 "ANTHROPIC_API_KEY", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.setattr(settings, name, None)


def test_testing_mode_hands_out_mocks():
    services_settings = server_provider_settings()
    assert services_settings.testing is True
    from flowforge_engine import ExecutionServices

    services = ExecutionServices(provider_settings=services_settings)
    for name in ("gemini", "groq", "openrouter", "ollama", "openai", "anthropic"):
        assert isinstance(services.llm(name), MockLLMProvider)
    assert isinstance(services.email("gmail"), MockEmailProvider)


def test_server_env_maps_to_provider_settings(monkeypatch):
    monkeypatch.setattr(settings, "TESTING", False)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", SecretStr("server-gemini-key-1234"))
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://host.docker.internal:11434/v1")
    monkeypatch.setattr(settings, "SMTP_USER", "server@gmail.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", SecretStr("server-app-password"))
    resolved = server_provider_settings()
    assert resolved.testing is False
    assert resolved.gemini.api_key.get_secret_value() == "server-gemini-key-1234"
    assert resolved.default_model("gemini") == "gemini-3.5-flash-lite"
    assert resolved.base_url("ollama") == "http://host.docker.internal:11434/v1"
    assert resolved.gmail.username == "server@gmail.com"


def test_user_credentials_take_priority(monkeypatch):
    monkeypatch.setattr(settings, "TESTING", False)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", SecretStr("server-gemini-key-1234"))
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setattr(settings, "SMTP_USER", "server@gmail.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", SecretStr("server-app-password"))

    resolved = provider_settings_for({
        "gemini": {"api_key": "user-gemini-key-5678"},
        "gmail": {"username": "user@gmail.com", "password": "user-app-password"},
    })
    assert resolved.gemini.api_key.get_secret_value() == "user-gemini-key-5678"
    assert resolved.default_model("gemini") == "gemini-3.5-flash-lite"  # server default model kept
    assert resolved.gmail.username == "user@gmail.com"
    assert resolved.gmail.password.get_secret_value() == "user-app-password"

    from flowforge_engine import ExecutionServices

    assert isinstance(ExecutionServices(provider_settings=resolved).llm("gemini"), GeminiProvider)


async def test_removing_the_key_is_an_authentication_missing_error(client, user, no_server_keys):
    wid = await create_workflow(client, user)

    validation = (await client.post(f"/api/workflows/{wid}/validate", headers=user.headers)).json()
    assert validation["valid"] is False
    by_node = {e["node_id"]: e for e in validation["errors"]}
    assert set(by_node) == {"gemini", "gmail"}
    assert {e["code"] for e in validation["errors"]} == {"auth_missing"}
    assert by_node["gemini"]["message"].startswith("Node 'gemini': Authentication missing for provider 'gemini'")
    assert "GEMINI_API_KEY" in by_node["gemini"]["message"]

    run = await client.post(f"/api/workflows/{wid}/run", json={"inputs": {"topic": "x"}}, headers=user.headers)
    assert run.status_code == 422
    assert "Authentication missing for provider 'gemini'" in run.text
    # Nothing ran: no silent mock email, no execution recorded.
    assert MockEmailProvider.outbox() == []
    history = await client.get(f"/api/workflows/{wid}/executions", headers=user.headers)
    assert history.json() == []


GEMINI_ONLY_GRAPH = {
    "nodes": [
        {"id": "input", "type": "input", "config": {"name": "topic", "default": "tides"}},
        {"id": "gemini", "type": "gemini", "config": {"user_prompt": "Summarize {{input.topic}}"}},
        {"id": "output", "type": "output", "config": {"value": "{{gemini.response}}"}},
    ],
    "edges": [{"source": "input", "target": "gemini"}, {"source": "gemini", "target": "output"}],
}


async def test_connecting_a_key_clears_the_error_for_that_user_only(client, user_factory, no_server_keys):
    alice, bob = await user_factory(), await user_factory()
    alice_wf = await create_workflow(client, alice, GEMINI_ONLY_GRAPH)
    bob_wf = await create_workflow(client, bob, GEMINI_ONLY_GRAPH)
    assert (await client.post(f"/api/workflows/{alice_wf}/validate", headers=alice.headers)).json()["valid"] is False

    connect = await client.post(
        "/api/integrations/gemini/connect", json={"api_key": "alice-key-abcdefghijkl"}, headers=alice.headers
    )
    assert connect.status_code == 200

    alice_validation = await client.post(f"/api/workflows/{alice_wf}/validate", headers=alice.headers)
    assert alice_validation.json() == {"valid": True, "errors": []}
    bob_validation = await client.post(f"/api/workflows/{bob_wf}/validate", headers=bob.headers)
    assert [e["code"] for e in bob_validation.json()["errors"]] == ["auth_missing"]


async def test_explicit_mock_provider_runs_without_keys(client, user, no_server_keys):
    graph = {
        **EXAMPLE_GRAPH,
        "nodes": [
            {**n, "config": {**n["config"], "provider": "mock"}} if n["id"] == "gemini"
            else {**n, "config": {**n["config"], "auth": "mock"}} if n["id"] == "gmail"
            else n
            for n in EXAMPLE_GRAPH["nodes"]
        ],
    }
    wid = await create_workflow(client, user, graph)
    run = await client.post(f"/api/workflows/{wid}/run", json={"inputs": {"topic": "tides"}}, headers=user.headers)
    assert run.status_code == 200, run.text
    by_key = {n["node_key"]: n for n in run.json()["node_executions"]}
    assert by_key["gemini"]["output"]["provider_used"] == "mock"
    assert by_key["gmail"]["output"]["mock"] is True
