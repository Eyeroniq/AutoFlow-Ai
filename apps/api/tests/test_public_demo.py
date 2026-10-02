"""PUBLIC_DEMO mode. The safety boundary: a visitor's nodes can use the owner's AI keys (capped), but
never the owner's Gmail, Discord, Telegram, Notion, or Airtable, by any path: runs, the node tester,
connection tests, triggers, listings, or a direct API request. The owner's own use is unchanged."""

import uuid

import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.models.enums import TriggerType
from app.services import demo
from app.services.credentials import (
    build_execution_services,
    credential_source,
    provider_settings_for,
    server_provider_settings,
)
from tests.support import create_workflow

PERSONAL = ["gmail", "telegram", "discord", "notion", "airtable"]


@pytest.fixture
def owner_accounts(monkeypatch):
    """The server's .env holds the owner's real accounts and AI keys. TESTING is off, so a missing
    credential is missing (in tests, TESTING hands everyone mock providers)."""
    monkeypatch.setattr(settings, "TESTING", False)
    monkeypatch.setattr(settings, "SMTP_USER", "owner@gmail.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", SecretStr("owner-app-password"))
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", SecretStr("123456789:" + "A" * 35))
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "424242")
    monkeypatch.setattr(settings, "DISCORD_WEBHOOK_URL", SecretStr("https://discord.com/api/webhooks/123456789012345678/ownerwebhooktoken"))
    monkeypatch.setattr(settings, "NOTION_API_KEY", SecretStr("secret_owner_notion"))
    monkeypatch.setattr(settings, "AIRTABLE_API_KEY", SecretStr("pat_owner_airtable"))
    monkeypatch.setattr(settings, "GEMINI_API_KEY", SecretStr("owner-gemini-key"))


@pytest.fixture
async def demo_mode(monkeypatch, owner_accounts, user_factory):
    """Demo mode on, with an owner account and a visitor account."""
    owner = await user_factory(email="owner@example.com")
    visitor = await user_factory(email="visitor@example.com")
    monkeypatch.setattr(settings, "PUBLIC_DEMO", True)
    monkeypatch.setattr(settings, "DEMO_OWNER_EMAIL", "Owner@Example.com")  # case-insensitive
    return owner, visitor


def gmail_graph():
    return {
        "nodes": [
            {"id": "mail", "type": "gmail", "config": {"to": "someone@example.com", "subject": "hi", "body": "hello"}},
        ],
        "edges": [],
    }


# --- Whose settings a run is built from ---------------------------------------------------------------


def test_a_visitors_settings_have_the_servers_ai_keys_and_none_of_the_owners_accounts(owner_accounts, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_DEMO", True)
    visitor = provider_settings_for({}, visitor=True)
    assert visitor.has_credentials("gemini")  # the shared demo AI key
    for provider in PERSONAL:
        assert not visitor.has_credentials(provider), f"{provider}: the owner's account leaked into a visitor's settings"
    owner = provider_settings_for({}, visitor=False)
    for provider in PERSONAL:
        assert owner.has_credentials(provider)
    assert owner.gmail.username == "owner@gmail.com"
    # The server-wide defaults themselves, as built for a visitor, hold nothing personal.
    bare = server_provider_settings(visitor=True)
    assert bare.gmail.username is None and bare.telegram.bot_token is None and bare.discord.webhook_url is None
    assert bare.notion.api_key is None and bare.airtable.api_key is None


def test_a_visitor_uses_only_their_own_personal_accounts_and_never_chooses_the_ai_key(owner_accounts, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_DEMO", True)
    own = {
        "gmail": {"username": "visitor@gmail.com", "password": "visitor-app-password"},
        "notion": {"api_key": "secret_visitor_notion"},
        "gemini": {"api_key": "visitor-gemini-key"},  # a stored AI key is ignored: the demo provides the AI
    }
    resolved = provider_settings_for(own, visitor=True)
    assert resolved.gmail.username == "visitor@gmail.com" and resolved.gmail.password.get_secret_value() == "visitor-app-password"
    assert resolved.notion.api_key.get_secret_value() == "secret_visitor_notion"
    assert resolved.llm_account("gemini").api_key.get_secret_value() == "owner-gemini-key"
    assert not resolved.has_credentials("telegram") and not resolved.has_credentials("airtable")


async def test_services_for_a_visitor_are_metered_and_for_the_owner_are_not(demo_mode, shared_session):
    owner, visitor = demo_mode
    from app.models.user import User

    owner_row, visitor_row = await shared_session.get(User, uuid.UUID(owner.id)), await shared_session.get(User, uuid.UUID(visitor.id))
    assert isinstance(await build_execution_services(shared_session, visitor_row), demo.MeteredServices)
    assert not isinstance(await build_execution_services(shared_session, owner_row), demo.MeteredServices)
    visitor_services = await build_execution_services(shared_session, visitor_row)
    assert not visitor_services.has_credentials("gmail") and visitor_services.has_credentials("gemini")
    from flowforge_engine.errors import MissingCredentialsError

    with pytest.raises(MissingCredentialsError):
        visitor_services.email("gmail")  # no way to obtain the owner's mailbox
    owner_services = await build_execution_services(shared_session, owner_row)
    assert owner_services.email("gmail").account.username == "owner@gmail.com"


def test_outside_demo_mode_everyone_keeps_the_server_accounts(owner_accounts):
    assert not settings.PUBLIC_DEMO and demo.is_owner_email("anyone@example.com")
    assert provider_settings_for({}).has_credentials("gmail")


# --- Through the API: no path hands a visitor the owner's account ---------------------------------------------


async def test_a_visitors_gmail_node_cannot_run_without_their_own_account(client, demo_mode, task_queue):
    owner, visitor = demo_mode
    wid = await create_workflow(client, visitor, gmail_graph())
    response = await client.post(f"/api/workflows/{wid}/run", json={"inputs": {}}, headers=visitor.headers)
    assert response.status_code == 422
    issue = response.json()["detail"]["errors"][0]
    assert issue["code"] == "auth_missing" and issue["node_id"] == "mail"
    assert "needs your own Gmail account" in issue["message"] and "SMTP_USER" not in issue["message"]  # advice for a visitor
    assert task_queue.enqueued == []  # nothing was queued, so nothing could send
    # The owner's identical workflow validates: the server's mailbox is theirs.
    owner_wid = await create_workflow(client, owner, gmail_graph())
    assert (await client.post(f"/api/workflows/{owner_wid}/run", json={"inputs": {}}, headers=owner.headers)).status_code == 202


async def test_validate_and_the_node_tester_agree_for_a_visitor(client, demo_mode):
    _, visitor = demo_mode
    wid = await create_workflow(client, visitor, gmail_graph())
    validation = (await client.post(f"/api/workflows/{wid}/validate", headers=visitor.headers)).json()
    assert [e["code"] for e in validation["errors"]] == ["auth_missing"]
    tested = await client.post(f"/api/workflows/{wid}/nodes/mail/test", json={}, headers=visitor.headers)
    body = tested.json()
    assert tested.status_code in (200, 422) and body.get("status") != "success"
    assert "owner@gmail.com" not in str(body)


async def test_a_visitor_with_their_own_credential_runs_it_with_that_one(client, demo_mode, shared_session):
    _, visitor = demo_mode
    connected = await client.post(
        "/api/integrations/gmail/connect",
        json={"email": "visitor@gmail.com", "app_password": "visitor-app-password"}, headers=visitor.headers,
    )
    assert connected.status_code == 200 and connected.json()["source"] == "user"
    from app.models.user import User

    row = await shared_session.get(User, uuid.UUID(visitor.id))
    services = await build_execution_services(shared_session, row)
    assert services.has_credentials("gmail") and services.email("gmail").account.username == "visitor@gmail.com"


async def test_integrations_for_a_visitor_list_only_personal_accounts_and_never_the_servers(client, demo_mode):
    owner, visitor = demo_mode
    listed = (await client.get("/api/integrations", headers=visitor.headers)).json()
    assert sorted(i["provider"] for i in listed) == sorted(PERSONAL)  # no AI providers to choose
    assert all(i["source"] == "none" and not i["connected"] for i in listed)
    seen_by_owner = {i["provider"]: i for i in (await client.get("/api/integrations", headers=owner.headers)).json()}
    assert all(seen_by_owner[p]["source"] == "server" for p in PERSONAL)  # the owner still has theirs
    assert seen_by_owner["gemini"]["source"] == "server"


async def test_a_visitor_cannot_store_an_ai_key_or_test_one(client, demo_mode):
    _, visitor = demo_mode
    refused = await client.post("/api/integrations/gemini/connect", json={"api_key": "AIza-visitor-key-0123456789"}, headers=visitor.headers)
    assert refused.status_code == 403 and "provides the AI itself" in refused.json()["detail"]


async def test_the_owners_stored_credential_is_invisible_and_untouchable_to_a_visitor(client, demo_mode):
    owner, visitor = demo_mode
    stored = await client.post(
        "/api/integrations/gmail/connect", json={"email": "owner-personal@gmail.com", "app_password": "owner-stored-password"},
        headers=owner.headers,
    )
    assert stored.status_code == 200
    seen = (await client.get("/api/integrations", headers=visitor.headers)).json()
    gmail = next(i for i in seen if i["provider"] == "gmail")
    assert gmail["connected"] is False and gmail["masked"] is None and gmail["source"] == "none"
    assert "owner" not in str(seen)
    # Direct requests by a visitor reach only the visitor's own (empty) rows.
    assert (await client.delete("/api/integrations/gmail", headers=visitor.headers)).status_code == 404
    tested = (await client.post("/api/integrations/gmail/test", headers=visitor.headers)).json()
    assert tested["success"] is False and tested["source"] == "none" and "owner" not in str(tested)
    still = next(i for i in (await client.get("/api/integrations", headers=owner.headers)).json() if i["provider"] == "gmail")
    assert still["connected"] is True  # the visitor's attempts changed nothing
    # And never by someone else's token: no credential endpoint takes a user id at all.
    assert (await client.get("/api/integrations")).status_code == 401


async def test_credential_source_never_says_server_for_a_visitors_personal_providers(owner_accounts, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_DEMO", True)
    for provider in PERSONAL:
        assert credential_source(provider, {}, visitor=True) == "none"
        assert credential_source(provider, {}, visitor=False) == "server"
    assert credential_source("gemini", {}, visitor=True) == "server"
    assert credential_source("gmail", {"gmail": {"username": "v@gmail.com"}}, visitor=True) == "user"


async def test_a_visitors_trigger_runs_use_the_visitors_services(client, demo_mode, shared_session):
    """Schedules, email triggers, and webhooks build their services from the workflow owner, so the same rule applies."""
    _, visitor = demo_mode
    from app.models.workflow import Workflow
    from app.services.triggers import _owner_services

    wid = await create_workflow(client, visitor, gmail_graph())
    workflow = await shared_session.get(Workflow, uuid.UUID(wid))
    services = await _owner_services(shared_session, workflow)
    assert not services.has_credentials("gmail") and not services.has_credentials("telegram")


# --- Caps -------------------------------------------------------------------------------------------------------------


async def test_a_visitor_has_a_daily_run_cap_and_the_owner_has_none(client, demo_mode, task_queue, monkeypatch):
    owner, visitor = demo_mode
    monkeypatch.setattr(settings, "TESTING", True)
    monkeypatch.setattr(settings, "DEMO_RUNS_PER_DAY", 2)
    graph = {"nodes": [{"id": "t", "type": "text", "config": {"text": "hello"}}], "edges": []}
    wid = await create_workflow(client, visitor, graph)
    codes = [(await client.post(f"/api/workflows/{wid}/run", json={"inputs": {}}, headers=visitor.headers)).status_code for _ in range(3)]
    assert codes == [202, 202, 422]
    limited = (await client.post(f"/api/workflows/{wid}/run", json={"inputs": {}}, headers=visitor.headers)).json()
    assert limited["detail"]["errors"][0]["code"] == "demo_limit" and "2 runs a day" in limited["detail"]["errors"][0]["message"]
    owner_wid = await create_workflow(client, owner, graph)
    assert [(await client.post(f"/api/workflows/{owner_wid}/run", json={"inputs": {}}, headers=owner.headers)).status_code for _ in range(4)] == [202] * 4
    other = await create_workflow(client, visitor, graph)  # the cap is per account, not per workflow
    assert (await client.post(f"/api/workflows/{other}/run", json={"inputs": {}}, headers=visitor.headers)).status_code == 422


async def test_a_visitors_ai_calls_count_against_a_token_budget(demo_mode, shared_session, monkeypatch):
    _, visitor = demo_mode
    monkeypatch.setattr(settings, "TESTING", True)
    monkeypatch.setattr(settings, "DEMO_TOKENS_PER_DAY", 200)
    from flowforge_engine import ProviderError

    from app.models.user import User

    row = await shared_session.get(User, uuid.UUID(visitor.id))
    services = await build_execution_services(shared_session, row)
    llm = services.llm("mock")
    assert isinstance(llm, demo.MeteredLLM) and llm.is_mock
    await llm.generate("system", "x" * 600, "m", 0.0, 100)  # about 150 tokens + the reply
    await llm.generate("system", "x" * 600, "m", 0.0, 100)
    with pytest.raises(ProviderError, match="today's are used up"):
        await llm.generate("system", "again", "m", 0.0, 100)
    assert (await demo.usage(row.id))["tokens"] >= 200


def test_token_estimates_are_about_four_characters_each():
    assert demo.estimate_tokens("a" * 400) == 100 and demo.estimate_tokens("") == 1


# --- The bots act for the owner only -------------------------------------------------------------------------------


async def test_the_telegram_command_center_answers_only_for_the_owners_pipelines(client, demo_mode, shared_session):
    from app.services.telegram_center import candidates_for
    from tests.test_telegram_center import CHAT, connect, greeting_graph

    owner, visitor = demo_mode
    await connect(client, owner, greeting_graph(), "Greets the owner by name")
    await connect(client, visitor, greeting_graph(), "Greets a visitor by name")
    found = await candidates_for(shared_session, CHAT)
    assert [c.name for c in found] == ["Greets the owner by name"[:40]]


async def test_the_discord_recorder_runs_only_the_owners_meeting_notes(client, demo_mode, shared_session):
    from app.services.discord_voice import find_pipeline
    from tests.test_discord_voice import meeting_graph

    owner, visitor = demo_mode
    await create_workflow(client, visitor, meeting_graph(), name="Meeting Notes")
    assert await find_pipeline(shared_session, "Meeting Notes") is None  # a visitor's workflow of that name is ignored
    await create_workflow(client, owner, meeting_graph(), name="Meeting Notes")
    pipeline = await find_pipeline(shared_session, "Meeting Notes")
    assert pipeline is not None and str(pipeline.owner.id) == owner.id


async def test_demo_mode_without_an_owner_leaves_nothing_to_the_bots(client, user, shared_session, monkeypatch):
    from app.services.discord_voice import find_pipeline
    from tests.test_discord_voice import meeting_graph

    monkeypatch.setattr(settings, "PUBLIC_DEMO", True)
    monkeypatch.setattr(settings, "DEMO_OWNER_EMAIL", "")
    await create_workflow(client, user, meeting_graph(), name="Meeting Notes")
    assert await find_pipeline(shared_session, "Meeting Notes") is None
    assert not demo.is_owner_email(user.email) and demo.is_visitor(type("U", (), {"email": user.email})())


# --- Telling the editor, health, and a production configuration --------------------------------------------------------


async def test_system_info_tells_the_editor_what_this_account_may_do(client, demo_mode):
    owner, visitor = demo_mode
    seen = (await client.get("/api/system", headers=visitor.headers)).json()
    assert seen["public_demo"] is True and seen["is_owner"] is False
    assert seen["own_account_providers"] == sorted(PERSONAL)
    assert seen["usage"]["runs_limit"] == settings.DEMO_RUNS_PER_DAY and seen["usage"]["runs"] >= 0
    mine = (await client.get("/api/system", headers=owner.headers)).json()
    assert mine["is_owner"] is True and mine["own_account_providers"] == [] and mine["usage"] is None


async def test_system_info_outside_demo_mode(client, user):
    seen = (await client.get("/api/system", headers=user.headers)).json()
    assert seen == {"public_demo": False, "is_owner": True, "own_account_providers": [], "usage": None}


def test_production_checks_refuse_unsafe_settings(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "JWT_SECRET", "change-me-please")
    monkeypatch.setattr(settings, "CORS_ORIGINS", "*, http://example.com")
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "PUBLIC_DEMO", True)
    monkeypatch.setattr(settings, "DEMO_OWNER_EMAIL", "")
    text = " | ".join(settings.production_problems())
    for fragment in ("JWT_SECRET", 'must not contain "*"', "plain-http origins", "COOKIE_SECURE", "DEMO_OWNER_EMAIL"):
        assert fragment in text
    monkeypatch.setattr(settings, "JWT_SECRET", "x" * 48)
    monkeypatch.setattr(settings, "CORS_ORIGINS", "https://demo.example.com")
    monkeypatch.setattr(settings, "COOKIE_SECURE", True)
    monkeypatch.setattr(settings, "DEMO_OWNER_EMAIL", "me@example.com")
    assert settings.production_problems() == []


async def test_health_reports_each_dependency_and_503s_when_one_is_down(client, monkeypatch):
    for path in ("/health", "/api/health"):
        ok = await client.get(path)
        assert ok.status_code == 200
        body = ok.json()
        assert body["status"] == "ok" and body["checks"] == {"database": "ok", "redis": "ok"} and body["database"] == "ok"
    import app.api.routes.health as health

    class Down:
        async def ping(self):
            raise ConnectionError("redis is down")

    monkeypatch.setattr(health, "get_redis", lambda: Down())
    down = await client.get("/health")
    assert down.status_code == 503 and down.json()["checks"] == {"database": "ok", "redis": "unavailable"}
    assert "redis is down" not in down.text  # no internals in a public endpoint


def test_the_server_wide_personal_settings_list_covers_every_account_env_key():
    """If a new personal account setting is added to the provider environment, it must be listed here
    (or a visitor would inherit it)."""
    env = settings.provider_env()
    personal_looking = {
        k for k in env if any(k.startswith(p) for p in ("SMTP_", "IMAP_", "TELEGRAM_", "DISCORD_", "NOTION_", "AIRTABLE_"))
    }
    assert personal_looking <= demo.PERSONAL_ENV_KEYS, personal_looking - demo.PERSONAL_ENV_KEYS
    assert {TriggerType.TELEGRAM.value, "gmail"} <= {"telegram", "gmail"}
