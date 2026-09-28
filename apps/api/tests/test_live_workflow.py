"""End-to-end against the real services, through the API. Run with:  pytest tests/test_live_workflow.py -m live -s

- Input -> Gemini -> Gmail(SMTP) -> Output sends a real email from SMTP_USER to SMTP_USER,
  then a Gmail Read (IMAP) workflow confirms it arrived in the inbox.
- /api/integrations/{provider}/test makes a real call with a real key.

Skipped (naming the missing variable) when keys are blank. Secrets are never printed.
"""

import asyncio
import copy
import uuid

import pytest

from app.core.config import settings
from app.schemas.workflow import EXAMPLE_GRAPH

pytestmark = pytest.mark.live

DELIVERY_TIMEOUT_SECONDS = 90


def require(*names):
    missing = [name for name in names if not getattr(settings, name)]
    if missing:
        pytest.skip(f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} blank; set it in .env to run this live test")


def report(label, **fields):
    print(f"\n[live] {label}: " + " | ".join(f"{k}={v}" for k, v in fields.items()))


@pytest.fixture
def real_providers(monkeypatch):
    # conftest sets TESTING=true for unit tests; live tests use the real providers.
    monkeypatch.setattr(settings, "TESTING", False)


async def create_workflow(client, user, name, graph):
    wid = (await client.post("/api/workflows", json={"name": name}, headers=user.headers)).json()["id"]
    response = await client.put(f"/api/workflows/{wid}", json={"graph": graph}, headers=user.headers)
    assert response.status_code == 200, response.text
    return wid


async def test_input_gemini_gmail_output_delivers_a_real_email(client, user, real_providers):
    require("GEMINI_API_KEY", "SMTP_USER", "SMTP_PASSWORD")
    marker = uuid.uuid4().hex[:8]
    graph = copy.deepcopy(EXAMPLE_GRAPH)
    graph["variables"] = [{"key": "recipient", "value": settings.SMTP_USER, "type": "workflow"}]
    for node in graph["nodes"]:
        if node["id"] == "gmail":
            node["config"]["subject"] = f"FlowForge live test {marker}: {{{{input.topic}}}}"
        if node["id"] == "gemini":
            node["config"]["max_tokens"] = 2048
    wid = await create_workflow(client, user, "Live: summarize and email", graph)

    validation = (await client.post(f"/api/workflows/{wid}/validate", headers=user.headers)).json()
    assert validation == {"valid": True, "errors": []}

    run = await client.post(
        f"/api/workflows/{wid}/run?sync=true", json={"inputs": {"topic": "renewable energy"}}, headers=user.headers
    )
    assert run.status_code == 200, run.text
    execution = run.json()
    nodes = {n["node_key"]: n for n in execution["node_executions"]}
    for key in ("gemini", "gmail"):
        node = nodes[key]
        report(f"run {key}", status=node["status"], duration_ms=node["duration_ms"], error=node["error_message"])
    assert execution["status"] == "success", execution["error_message"]

    gemini, gmail = nodes["gemini"]["output"], nodes["gmail"]["output"]
    report("gemini output", provider_used=gemini["provider_used"], model=gemini["model"], mock=gemini["mock"],
           response=repr(gemini["response"][:200]))
    report("gmail output", status=gmail["status"], message_id=gmail["message_id"], subject=gmail["subject"],
           mock=gmail["mock"])
    assert gemini["provider_used"] == "gemini" and gemini["mock"] is False
    assert gmail["status"] == "sent" and gmail["mock"] is False
    assert execution["final_output"]["result"]["summary"] == gemini["response"]

    # Delivery: read the inbox over IMAP with a Gmail Read workflow until the message shows up.
    reader = await create_workflow(client, user, "Live: find the test email", {
        "nodes": [
            {"id": "gmail_read", "type": "gmail_read",
             "config": {"subject": marker, "unread_only": False, "since_days": 1, "max_results": 5}},
            {"id": "out", "type": "output", "config": {"value": "{{gmail_read.emails}}"}},
        ],
        "edges": [{"source": "gmail_read", "target": "out"}],
    })
    deadline = asyncio.get_running_loop().time() + DELIVERY_TIMEOUT_SECONDS
    emails = []
    while True:
        read = (await client.post(f"/api/workflows/{reader}/run?sync=true", json={}, headers=user.headers)).json()
        assert read["status"] == "success", read["error_message"]
        emails = read["final_output"]["result"]
        if emails or asyncio.get_running_loop().time() > deadline:
            break
        await asyncio.sleep(5)
    assert emails, f"email '{marker}' not found in the inbox within {DELIVERY_TIMEOUT_SECONDS}s"
    report("inbox (IMAP)", found=len(emails), subject=emails[0]["subject"], from_=emails[0]["from_address"],
           snippet=repr(emails[0]["snippet"][:120]))
    assert gemini["response"].split()[0] in emails[0]["body_text"]


async def test_gemini_connection_test_with_a_real_key(client, user, real_providers):
    require("GEMINI_API_KEY")
    # Store the key as this user's own credential, then test it for real.
    key = settings.GEMINI_API_KEY.get_secret_value()
    connect = await client.post("/api/integrations/gemini/connect", json={"api_key": key}, headers=user.headers)
    assert connect.status_code == 200 and key not in connect.text
    result = (await client.post("/api/integrations/gemini/test", headers=user.headers)).json()
    report("POST /api/integrations/gemini/test", success=result["success"], source=result["source"],
           latency_ms=result["latency_ms"], details=result["details"], error=result["error"])
    assert result["success"] is True and result["source"] == "user"
    assert key not in str(result)


async def test_gmail_connection_test_with_server_credentials(client, user, real_providers):
    require("SMTP_USER", "SMTP_PASSWORD")
    result = (await client.post("/api/integrations/gmail/test", headers=user.headers)).json()
    report("POST /api/integrations/gmail/test", success=result["success"], source=result["source"],
           latency_ms=result["latency_ms"], details=result["details"], error=result["error"])
    assert settings.SMTP_PASSWORD.get_secret_value() not in str(result)
    assert result["success"] is True, result["error"]
