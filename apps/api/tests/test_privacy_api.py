"""The privacy layer through the API: masking stored step data, the Privacy Report, the
guard in real runs, the settings endpoint, and Redact/Restore plus masked outputs surviving
queue hand-offs (the vault and the stash live in Redis)."""

import json

from flowforge_engine.privacy import verhoeff_digit
from flowforge_engine.providers import MockEmailProvider
from sqlalchemy import select

from app.models.execution import NodeExecution
from tests.support import create_workflow, queue_run
from tests.test_queue_handoff import segment

CARD = "4111 1111 1111 1111"
AADHAAR = "2345 6789 012" + verhoeff_digit("23456789012")


def email_graph(guard: str = "redact"):
    return {
        "nodes": [
            {"id": "msg", "type": "input", "config": {"name": "message"}},
            {"id": "mail", "type": "gmail", "config": {
                "auth": "mock", "to": "boss@example.com", "subject": "Customer details", "body": "{{msg.value}}",
                "privacy_guard": guard}},
            {"id": "out", "type": "output", "config": {"value": {"sent": "{{mail.status}}", "echo": "{{msg.value}}"}}},
        ],
        "edges": [{"source": "msg", "target": "mail"}, {"source": "mail", "target": "out"}],
    }


async def run_sync(client, user, wid, message):
    response = await client.post(f"/api/workflows/{wid}/run?sync=true", json={"inputs": {"message": message}},
                                 headers=user.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_redacted_email_masked_storage_and_the_privacy_report(client, user):
    wid = await create_workflow(client, user, email_graph("redact"))
    message = f"Card {CARD}, Aadhaar {AADHAAR}"
    run = await run_sync(client, user, wid, message)
    assert run["status"] == "success", run

    [sent] = MockEmailProvider.outbox()
    assert sent.body == "Card [REDACTED:CREDIT_CARD], Aadhaar [REDACTED:AADHAAR]"

    # Nothing stored or returned holds the values: rows, final output, report.
    detail = (await client.get(f"/api/executions/{run['id']}", headers=user.headers)).json()
    assert CARD not in json.dumps(detail) and AADHAAR not in json.dumps(detail)
    nodes = {n["node_key"]: n for n in detail["node_executions"]}
    assert nodes["msg"]["output"]["value"] == "Card [REDACTED:CREDIT_CARD], Aadhaar [REDACTED:AADHAAR]"
    assert nodes["mail"]["privacy"]["guard"]["action"] == "redacted"
    assert detail["final_output"]["result"]["echo"].startswith("Card [REDACTED:CREDIT_CARD]")

    report = detail["privacy_report"]
    assert report["masked"] is True and report["guard_actions"] == {"redacted": 1}
    # One card and one Aadhaar, though they pass through two steps (and two keys of the Input's output).
    assert report["by_type"] == {"CREDIT_CARD": 1, "AADHAAR": 1} and report["total"] == 2
    assert report["by_category"].keys() >= {"financial", "government_id"}
    seen = {n["node_key"] for n in report["nodes"]}
    assert {"msg", "mail"} <= seen
    assert next(n for n in report["nodes"] if n["node_key"] == "mail")["guard"]["fields"] == ["body"]


async def test_blocked_email_fails_the_run_with_types_only(client, user):
    wid = await create_workflow(client, user, email_graph("block"))
    run = await run_sync(client, user, wid, f"Card {CARD}")
    assert run["status"] == "failed"
    mail = next(n for n in run["node_executions"] if n["node_key"] == "mail")
    assert "Blocked by the privacy guard: found 1 card number in body" in mail["error_message"]
    assert CARD not in json.dumps(run) and MockEmailProvider.outbox() == []
    detail = (await client.get(f"/api/executions/{run['id']}", headers=user.headers)).json()
    assert detail["privacy_report"]["guard_actions"] == {"blocked": 1}


async def test_settings_turn_masking_off_and_personal_data_on(client, user):
    wid = await create_workflow(client, user, email_graph("warn"))
    defaults = (await client.get(f"/api/workflows/{wid}/privacy", headers=user.headers)).json()
    assert defaults == {"mask_stored_io": True, "detect_personal_data": False, "allowlist": []}
    bad = await client.put(f"/api/workflows/{wid}/privacy", json={"allowlist": ["re:(unclosed"]}, headers=user.headers)
    assert bad.status_code == 422
    saved = await client.put(f"/api/workflows/{wid}/privacy",
                             json={"mask_stored_io": False, "detect_personal_data": True, "allowlist": ["  ", "ok@example.com"]},
                             headers=user.headers)
    assert saved.json() == {"mask_stored_io": False, "detect_personal_data": True, "allowlist": ["ok@example.com"]}

    run = await run_sync(client, user, wid, "Ping priya@example.com, not ok@example.com")
    assert run["status"] == "success"
    detail = (await client.get(f"/api/executions/{run['id']}", headers=user.headers)).json()
    msg = next(n for n in detail["node_executions"] if n["node_key"] == "msg")
    assert msg["output"]["value"] == "Ping priya@example.com, not ok@example.com"  # masking off: stored as is
    report = detail["privacy_report"]
    assert report["masked"] is False and report["by_type"].get("EMAIL", 0) >= 1
    mail = next(n for n in report["nodes"] if n["node_key"] == "mail")
    assert mail["guard"]["action"] == "warned" and mail["guard"]["by_type"] == {"EMAIL": 1}  # the allowlisted one isn't counted


async def test_other_users_cant_read_or_change_privacy_settings(client, user, user_factory):
    wid = await create_workflow(client, user, email_graph())
    other = await user_factory()
    assert (await client.get(f"/api/workflows/{wid}/privacy", headers=other.headers)).status_code == 404
    assert (await client.put(f"/api/workflows/{wid}/privacy", json={}, headers=other.headers)).status_code == 404


def handoff_graph():
    """Text (a card) -> Redact -> Gemini (llm queue) -> Restore -> Gmail (default queue, warn):
    the run moves default -> llm -> default; the vault and the stash must carry over."""
    return {
        "nodes": [
            {"id": "text", "type": "text", "config": {"text": f"Card {CARD} for asha@example.com"}},
            {"id": "redact", "type": "pii_redact", "config": {"text": "{{text.text}}"}},
            {"id": "llm", "type": "gemini", "config": {"provider": "mock", "user_prompt": "{{redact.text}}", "privacy_guard": "off"}},
            {"id": "restore", "type": "pii_restore", "config": {"text": "{{redact.text}}", "mapping_id": "{{redact.mapping_id}}"}},
            {"id": "mail", "type": "gmail", "config": {
                "auth": "mock", "to": "boss@example.com", "subject": "x", "body": "{{restore.text}} / {{text.text}}",
                "privacy_guard": "warn"}},
        ],
        "edges": [{"source": "text", "target": "redact"}, {"source": "redact", "target": "llm"},
                  {"source": "llm", "target": "restore"}, {"source": "restore", "target": "mail"}],
    }


async def test_redact_restore_and_masked_outputs_survive_queue_handoffs(
    client, user, task_queue, session_factory, redis, shared_session
):
    wid = await create_workflow(client, user, handoff_graph())
    execution_id = await queue_run(client, user, wid)
    assert task_queue.enqueued == [(execution_id, "default")]
    first = await segment(execution_id, session_factory, redis, task_queue, worker="d@1", queue="default")
    assert first["handed_off_to"] == "llm"
    second = await segment(execution_id, session_factory, redis, task_queue, worker="l@1", queue="llm", number=1)
    assert second["handed_off_to"] == "default"
    third = await segment(execution_id, session_factory, redis, task_queue, worker="d@2", queue="default", number=2)
    assert third["status"] == "success", third

    # Another worker restored the placeholders (vault), and the Text step's real output
    # reached Gmail although its row is masked (stash).
    [sent] = MockEmailProvider.outbox()
    assert sent.body == f"Card {CARD} for asha@example.com / Card {CARD} for asha@example.com"
    rows = {r.node_key: r for r in await shared_session.scalars(
        select(NodeExecution).where(NodeExecution.execution_id == execution_id).execution_options(populate_existing=True))}
    assert rows["text"].output_json["text"] == "Card [REDACTED:CREDIT_CARD] for asha@example.com"
    assert "<CREDIT_CARD_1>" in rows["redact"].output_json["text"] and CARD not in json.dumps(rows["redact"].output_json)
    # The stash is gone once the run finished.
    assert await redis.exists(f"flowforge:execution:{execution_id}:raw-outputs") == 0
