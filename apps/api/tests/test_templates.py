"""Templates (catalog sync, requirements, "Use template"), node state between runs, the
final output download, and Telegram/Discord credentials."""

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from flowforge_engine import ExecutionServices, ProviderSettings, WorkflowGraph, validate_workflow
from sqlalchemy import select

from app.models.enums import ExecutionStatus
from app.models.execution import WorkflowExecution
from app.models.template import Template
from app.models.trigger import NodeState, WorkflowTrigger
from app.services.exports import output_rows, to_csv
from app.services.node_state import DbNodeStateStore
from app.services.templates import CATALOG, fit_graph, sync_templates
from tests.support import create_workflow

TEMPLATE_SLUGS = [
    "morning-digest", "invoice-extractor", "email-triage", "job-alert-filter", "meeting-notes", "web-research",
    "pdf-to-knowledge-base", "document-qa",
]


@pytest.fixture
async def catalog(db_session):
    await sync_templates(db_session)


async def test_catalog_lists_templates_with_their_requirements(client, user, catalog):
    response = await client.get("/api/templates", headers=user.headers)
    assert response.status_code == 200
    templates = {t["slug"]: t for t in response.json()}
    assert list(templates) == TEMPLATE_SLUGS
    digest = templates["morning-digest"]
    assert digest["node_types"] == ["gmail_read", "rss", "for_each", "join", "gemini", "telegram", "output"]
    assert [r["providers"] for r in digest["requirements"]] == [["gmail"], ["gemini", "groq", "openrouter"], ["telegram", "discord"]]
    assert digest["triggers"] == [{"type": "schedule", "config": {"cron": "30 7 * * *", "timezone": "UTC", "inputs": {}}}]
    assert templates["invoice-extractor"]["requirements"][0]["label"].startswith("An LLM key")
    assert all(isinstance(r["satisfied"], bool) for t in templates.values() for r in t["requirements"])


async def test_sync_is_idempotent(db_session, catalog):
    await sync_templates(db_session)
    rows = list(await db_session.scalars(select(Template).where(Template.slug.is_not(None))))
    assert sorted(r.slug for r in rows) == sorted(TEMPLATE_SLUGS)


@pytest.mark.parametrize("entry", CATALOG, ids=[e["slug"] for e in CATALOG])
def test_every_template_graph_validates(entry):
    graph = WorkflowGraph.model_validate(entry["graph"])
    assert validate_workflow(graph, services=ExecutionServices(provider_settings=ProviderSettings(testing=True))) == []


@pytest.mark.parametrize("slug", TEMPLATE_SLUGS)
async def test_use_template_creates_an_editable_workflow(client, user, catalog, db_session, slug):
    response = await client.post(f"/api/templates/{slug}/use", json={"timezone": "Asia/Kolkata"}, headers=user.headers)
    assert response.status_code == 201, response.text
    workflow = response.json()
    assert workflow["version"] == 1 and workflow["status"] == "active"
    valid = await client.post(f"/api/workflows/{workflow['id']}/validate", headers=user.headers)
    assert valid.json() == {"valid": True, "errors": []}

    triggers = list(await db_session.scalars(
        select(WorkflowTrigger).where(WorkflowTrigger.workflow_id == uuid.UUID(workflow["id"]))
    ))
    assert all(not t.enabled for t in triggers)  # created switched off
    template = next(e for e in CATALOG if e["slug"] == slug)
    assert [t.type.value for t in triggers] == [t["type"] for t in template["triggers"]]
    for trigger in triggers:
        if trigger.type.value == "schedule":
            assert trigger.config_json["timezone"] == "Asia/Kolkata"

    # Editable like any workflow, and a second copy gets its own name.
    renamed = await client.put(f"/api/workflows/{workflow['id']}", json={"name": "Mine"}, headers=user.headers)
    assert renamed.status_code == 200
    again = (await client.post(f"/api/templates/{slug}/use", headers=user.headers)).json()
    assert again["name"] == template["name"]
    third = (await client.post(f"/api/templates/{slug}/use", headers=user.headers)).json()
    assert third["name"] == f"{template['name']} (2)"


async def test_invoice_template_defaults_to_the_sample_scan(client, user, catalog):
    workflow = (await client.post("/api/templates/invoice-extractor/use", headers=user.headers)).json()
    node = next(n for n in workflow["graph"]["nodes"] if n["type"] == "input")
    file_id = node["config"]["default"]
    stored = await client.get(f"/api/files/{file_id}", headers=user.headers)
    assert stored.status_code == 200 and stored.json()["filename"] == "scanned-invoice.pdf"


async def test_meeting_notes_defaults_to_the_sample_recording(client, user, catalog):
    workflow = (await client.post("/api/templates/meeting-notes/use", headers=user.headers)).json()
    node = next(n for n in workflow["graph"]["nodes"] if n["type"] == "input")
    stored = (await client.get(f"/api/files/{node['config']['default']}", headers=user.headers)).json()
    assert stored["filename"] == "team-meeting.mp3" and stored["content_type"] == "audio/mpeg"


async def test_audio_and_research_templates_state_their_credentials(client, user, catalog):
    templates = {t["slug"]: t for t in (await client.get("/api/templates", headers=user.headers)).json()}
    notes = templates["meeting-notes"]
    assert notes["node_types"] == ["input", "speech_to_text", "structured_output", "join", "join", "telegram", "output"]
    assert [r["providers"] for r in notes["requirements"]] == [
        ["groq", "local"], ["gemini", "groq", "openrouter"], ["telegram", "discord", "gmail"]]
    research = templates["web-research"]
    assert research["node_types"] == ["input", "web_search", "web_page", "join", "gemini", "output"]
    assert [r["providers"] for r in research["requirements"]] == [["duckduckgo", "tavily"], ["gemini", "groq", "openrouter"]]


def test_fit_graph_for_meeting_notes():
    graph = next(e for e in CATALOG if e["slug"] == "meeting-notes")["graph"]

    def services(**accounts):
        return ExecutionServices(provider_settings=ProviderSettings(**accounts))

    # No Groq key: local faster-whisper; the structured-output step uses what's there.
    nodes = {n["id"]: n for n in fit_graph(graph, services(gemini={"api_key": "g"}, telegram={"bot_token": "1:x"}))["nodes"]}
    assert nodes["stt"]["config"]["provider"] == "local"
    assert nodes["notes"]["config"]["provider"] == "gemini" and nodes["send"]["type"] == "telegram"
    assert "email_subject" not in nodes["send"]

    # Groq only, and only Gmail to deliver: the notes are emailed to the user.
    fitted = fit_graph(graph, services(groq={"api_key": "q"}, gmail={"username": "me@gmail.com", "password": "x"}),
                       email_to="me@example.com")
    nodes = {n["id"]: n for n in fitted["nodes"]}
    assert nodes["stt"]["config"]["provider"] == "groq" and nodes["notes"]["config"]["provider"] == "groq"
    assert nodes["send"]["type"] == "gmail" and nodes["send"]["config"]["to"] == "me@example.com"
    assert nodes["send"]["config"]["subject"] == "Meeting notes: {{stt.filename}}"
    assert "{{notes.data.summary}}" in nodes["send"]["config"]["body"]
    assert validate_workflow(WorkflowGraph.model_validate(fitted),
                             services=ExecutionServices(provider_settings=ProviderSettings(testing=True))) == []

    # Discord only.
    nodes = {n["id"]: n for n in fit_graph(graph, services(groq={"api_key": "q"}, discord={"webhook_url": "u"}))["nodes"]}
    assert nodes["send"]["type"] == "discord_webhook"


def test_fit_graph_uses_the_providers_you_have():
    graph = next(e for e in CATALOG if e["slug"] == "morning-digest")["graph"]

    def services(**accounts):
        return ExecutionServices(provider_settings=ProviderSettings(**accounts))

    groq_only = fit_graph(graph, services(groq={"api_key": "gsk_x"}, telegram={"bot_token": "1:x"}))
    nodes = {n["id"]: n for n in groq_only["nodes"]}
    assert nodes["digest"]["type"] == "groq" and nodes["digest"]["config"]["provider"] == "groq"
    assert nodes["summaries"]["config"]["provider"] == "groq" and nodes["summaries"]["config"]["fallback"] == []
    assert nodes["send"]["type"] == "telegram"

    both = fit_graph(graph, services(gemini={"api_key": "g"}, groq={"api_key": "q"}, discord={"webhook_url": "u"}))
    nodes = {n["id"]: n for n in both["nodes"]}
    assert nodes["digest"]["type"] == "gemini" and nodes["digest"]["config"]["fallback"] == ["groq"]
    # Only Discord: the Telegram step becomes a Discord Webhook step with the same text.
    assert nodes["send"]["type"] == "discord_webhook" and nodes["send"]["label"] == "Send to Discord"
    assert nodes["send"]["config"] == {"content": "☀️ **Morning digest**\n\n{{digest.response}}"}
    assert graph["nodes"][4]["type"] == "gemini"  # the catalog itself is untouched


async def test_unknown_template(client, user, catalog):
    assert (await client.post("/api/templates/nope/use", headers=user.headers)).status_code == 404


# --- node state --------------------------------------------------------------------------------


async def test_node_state_moves_forward_only_with_successful_runs(client, user, db_session, session_factory):
    wid = uuid.UUID(await create_workflow(client, user, {"nodes": [], "edges": []}, name="State"))
    base = datetime.now(UTC)

    async def execution(status: ExecutionStatus, minutes: int) -> uuid.UUID:
        row = WorkflowExecution(workflow_id=wid, status=status, created_at=base + timedelta(minutes=minutes))
        db_session.add(row)
        await db_session.commit()
        return row.id

    first = await execution(ExecutionStatus.SUCCESS, 0)
    await DbNodeStateStore(session_factory, wid, first).save("news", {"seen": ["a"]})
    failed = await execution(ExecutionStatus.FAILED, 1)
    await DbNodeStateStore(session_factory, wid, failed).save("news", {"seen": ["a", "b"]})
    reader = DbNodeStateStore(session_factory, wid, None)
    assert await reader.load("news") == {"seen": ["a"]}  # the failed run didn't count
    assert await reader.load("other") is None

    await reader.save("news", {"seen": ["zzz"]})  # read-only: a test run saves nothing
    latest = await execution(ExecutionStatus.SUCCESS, 2)
    await DbNodeStateStore(session_factory, wid, latest).save("news", {"seen": ["a", "b", "c"]})
    assert await reader.load("news") == {"seen": ["a", "b", "c"]}
    # Rows older than the newest successful one are pruned.
    rows = list(await db_session.scalars(select(NodeState).where(NodeState.workflow_id == wid)))
    assert [r.state_json for r in rows] == [{"seen": ["a", "b", "c"]}]


# --- the final output download -------------------------------------------------------------------

INVOICE_OUTPUT = {
    "invoice": {
        "file": "scan.pdf",
        "pages": 1,
        "entities": {
            "organizations": [{"name": "Acme GmbH", "kind": "company"}],
            "amounts": [{"text": "EUR 4,389.20", "value": 4389.2, "currency": "EUR", "meaning": "total due"}],
            "dates": [],
            "custom": {"invoice_number": [{"text": "INV-2291", "note": None}]},
        },
    }
}


def test_output_rows_flatten_any_json():
    rows = output_rows(INVOICE_OUTPUT)
    assert rows == [
        {"group": "invoice.file", "value": "scan.pdf"},
        {"group": "invoice.pages", "value": 1},
        {"group": "invoice.entities.organizations", "name": "Acme GmbH", "kind": "company"},
        {"group": "invoice.entities.amounts", "text": "EUR 4,389.20", "value": 4389.2, "currency": "EUR", "meaning": "total due"},
        {"group": "invoice.entities.custom.invoice_number", "text": "INV-2291", "note": ""},
    ]
    table = list(csv.DictReader(io.StringIO(to_csv(INVOICE_OUTPUT))))
    assert list(table[0]) == ["group", "value", "name", "kind", "text", "currency", "meaning", "note"]
    assert table[3]["currency"] == "EUR" and table[3]["value"] == "4389.2"


async def test_download_endpoint(client, user, db_session):
    wid = uuid.UUID(await create_workflow(client, user, {"nodes": [], "edges": []}, name="Out"))
    row = WorkflowExecution(workflow_id=wid, status=ExecutionStatus.SUCCESS, final_output_json=INVOICE_OUTPUT)
    empty = WorkflowExecution(workflow_id=wid, status=ExecutionStatus.FAILED)
    db_session.add_all([row, empty])
    await db_session.commit()

    as_csv = await client.get(f"/api/executions/{row.id}/output?format=csv", headers=user.headers)
    assert as_csv.status_code == 200 and as_csv.headers["content-type"].startswith("text/csv")
    assert as_csv.headers["content-disposition"] == f'attachment; filename="execution-{str(row.id)[:8]}-output.csv"'
    assert as_csv.text.startswith("﻿group,value,")
    as_json = await client.get(f"/api/executions/{row.id}/output", headers=user.headers)
    assert as_json.json() == INVOICE_OUTPUT
    assert (await client.get(f"/api/executions/{empty.id}/output", headers=user.headers)).status_code == 404

    other = await client.post("/api/auth/register", json={"email": f"x-{uuid.uuid4().hex[:6]}@example.com",
                                                           "password": "password123", "full_name": "X"})
    stranger = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert (await client.get(f"/api/executions/{row.id}/output", headers=stranger)).status_code == 404


# --- Telegram and Discord credentials ------------------------------------------------------------

BOT_TOKEN = "7123456789:AAFakeTokenForTests_abcdefghijklmno"
WEBHOOK = "https://discord.com/api/webhooks/112233445566778899/FakeWebhookToken-abcdefghijklmnopqrstuvwxyz"


async def test_telegram_and_discord_credentials_are_stored_masked(client, user):
    telegram = await client.post("/api/integrations/telegram/connect", json={"bot_token": BOT_TOKEN, "chat_id": "42"},
                                 headers=user.headers)
    assert telegram.status_code == 200, telegram.text
    assert BOT_TOKEN not in telegram.text
    assert telegram.json()["masked"] == {"bot_token": "712...lmno", "chat_id": "42"} and telegram.json()["source"] == "user"

    discord = await client.post("/api/integrations/discord/connect", json={"webhook_url": WEBHOOK}, headers=user.headers)
    assert discord.status_code == 200 and "FakeWebhookToken" not in discord.text
    assert discord.json()["masked"] == {"webhook_url": "https://discord.com/api/webhooks/112233445566778899/****"}

    tested = await client.post("/api/integrations/telegram/test", headers=user.headers)
    assert tested.json()["success"] is True and tested.json()["details"]["chat"] == {"id": "42"}


@pytest.mark.parametrize(
    ("provider", "body", "message"),
    [
        ("telegram", {"bot_token": "not-a-token"}, "doesn't look like a Telegram bot token"),
        ("telegram", {"chat_id": "42"}, "needs a 'bot_token'"),
        ("telegram", {"bot_token": BOT_TOKEN, "api_key": "x"}, "doesn't take api_key"),
        ("discord", {"webhook_url": "https://example.com/hook"}, "not a Discord webhook URL"),
        ("gmail", {"email": "me@gmail.com", "app_password": "x", "bot_token": BOT_TOKEN}, "doesn't take bot_token"),
    ],
)
async def test_messaging_credential_validation(client, user, provider, body, message):
    response = await client.post(f"/api/integrations/{provider}/connect", json=body, headers=user.headers)
    assert response.status_code == 422 and message in response.json()["detail"]
    assert BOT_TOKEN not in response.text


async def test_a_users_bot_replaces_the_servers_including_the_chat(monkeypatch):
    from app.services import credentials

    monkeypatch.setattr(credentials, "server_provider_settings",
                        lambda: ProviderSettings(telegram={"bot_token": "1:server", "chat_id": "server-chat"}))
    merged = credentials.provider_settings_for({"telegram": {"bot_token": BOT_TOKEN}})
    assert merged.telegram.bot_token.get_secret_value() == BOT_TOKEN and merged.telegram.chat_id is None
    assert credentials.provider_settings_for({}).telegram.chat_id == "server-chat"


async def test_knowledge_templates_create_the_default_knowledge_base_once(client, user, catalog):
    for slug in ("pdf-to-knowledge-base", "document-qa"):
        response = await client.post(f"/api/templates/{slug}/use", json={}, headers=user.headers)
        assert response.status_code == 201, response.text
        assert response.json()["graph"]["variables"][0] == {"key": "knowledge_base", "value": "My documents", "type": "workflow"}
    bases = (await client.get("/api/knowledge-bases", headers=user.headers)).json()
    assert [kb["name"] for kb in bases] == ["My documents"]
