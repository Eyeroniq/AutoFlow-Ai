"""Telegram Command Center: update_id de-duplication, the allowlist, the intent router (clear
vs ambiguous input, missing inputs), signed confirmations and their expiry, side-effect
detection, voice notes and photos, the privacy checks, and the per-chat rate limit. The bot is
a recording fake; runs execute in-process on the real database and Redis (mock providers)."""

import json
import time
import uuid
from typing import Any

import pytest
from flowforge_engine import WorkflowGraph
from flowforge_engine.providers import MockEmailProvider

from app.core.config import settings
from app.services.deployments import has_side_effects
from app.services.runs import run_execution
from app.services.telegram_center import (
    Candidate,
    CommandCenter,
    ConfirmationError,
    callback_data,
    decide,
    verify_callback,
)
from tests.support import create_workflow
from tests.test_deployments import deploy

CHAT, OTHER_CHAT = "424242", "999"


class FakeBot:
    """Records what the bot would send; serves files from `files`."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.answers: list[tuple[str, str | None]] = []
        self.edits: list[tuple[str, int, str]] = []
        self.files: dict[str, bytes] = {}

    async def send_message(self, chat_id, text, **options):
        self.sent.append({"chat_id": str(chat_id), "text": text, **options})
        return {"message_id": len(self.sent)}

    async def answer_callback_query(self, query_id, text=None, *, alert=False):
        self.answers.append((query_id, text))

    async def edit_message_text(self, chat_id, message_id, text, **options):
        self.edits.append((str(chat_id), message_id, text))

    async def send_document(self, chat_id, filename, data, content_type, *, caption=None, **_):
        self.sent.append({"chat_id": str(chat_id), "text": caption, "document": filename, "data": data,
                          "content_type": content_type})
        return {"message_id": len(self.sent)}

    async def send_photo(self, chat_id, filename, data, content_type, *, caption=None, **_):
        self.sent.append({"chat_id": str(chat_id), "text": caption, "photo": filename, "data": data,
                          "content_type": content_type})
        return {"message_id": len(self.sent)}

    async def download_file(self, file_id, **_):
        return self.files[file_id], f"files/{file_id}"

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]


class InlineQueue:
    """Runs each queued execution right away, in-process (a worker for every queue)."""

    def __init__(self, session_factory, redis):
        self.session_factory, self.redis, self.enqueued = session_factory, redis, []

    async def enqueue(self, execution_id, queue, *, segment=0):
        self.enqueued.append(execution_id)
        await run_execution(execution_id, session_factory=self.session_factory, redis=self.redis, worker_id="test@1")
        return str(execution_id)

    async def revoke(self, task_id):
        return None


def router(*replies):
    """A scripted LLM for the router: each call returns the next reply (dict -> JSON)."""
    queue = list(replies)
    prompts: list[str] = []

    def factory(_owner):
        async def call(system, prompt):
            prompts.append(prompt)
            reply = queue.pop(0)
            return json.dumps(reply) if isinstance(reply, dict) else reply

        return call

    factory.prompts = prompts  # type: ignore[attr-defined]
    return factory


def greeting_graph():
    return {
        "nodes": [
            {"id": "who", "type": "input", "config": {"name": "name"}},
            {"id": "text", "type": "text", "config": {"text": "Hello, {{who.value}}!"}},
            {"id": "out", "type": "output", "config": {"value": "{{text.text}}"}},
        ],
        "edges": [{"source": "who", "target": "text"}, {"source": "text", "target": "out"}],
    }


def email_graph():
    return {
        "nodes": [
            {"id": "note", "type": "input", "config": {"name": "note"}},
            {"id": "mail", "type": "gmail", "config": {"auth": "mock", "to": "boss@example.com", "subject": "Note",
                                                       "body": "{{note.value}}"}},
        ],
        "edges": [{"source": "note", "target": "mail"}],
    }


def photo_graph():
    return {
        "nodes": [
            {"id": "pic", "type": "input", "config": {"name": "photo", "input_type": "file"}},
            {"id": "out", "type": "output", "config": {"value": "{{pic.value.filename}}"}},
        ],
        "edges": [{"source": "pic", "target": "out"}],
    }


async def connect(client, user, graph, description, chats=(CHAT,)):
    """Create, deploy, and switch on the Telegram trigger; returns (workflow id, deployment)."""
    wid = await create_workflow(client, user, graph, name=description[:40])
    deployment = await deploy(client, user, wid, expect=201, description=description)
    response = await client.put(f"/api/workflows/{wid}/triggers/telegram",
                                json={"enabled": True, "config": {"allowed_chat_ids": list(chats)}}, headers=user.headers)
    assert response.status_code == 200, response.text
    return wid, deployment


def message(update_id, text=None, chat=CHAT, **extra):
    body = {"message_id": update_id, "chat": {"id": int(chat), "type": "private"}, "date": int(time.time()), **extra}
    if text is not None:
        body["text"] = text
    return {"update_id": update_id, "message": body}


def tap(update_id, data, chat=CHAT, message_id=7):
    return {"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "data": data, "message": {"message_id": message_id, "chat": {"id": int(chat)}}}}


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture
def make_center(bot, session_factory, redis):
    def make(llm=None, **kwargs):
        return CommandCenter(bot, session_factory=session_factory, redis=redis,
                             task_queue=InlineQueue(session_factory, redis), llm_factory=llm, wait_seconds=10, **kwargs)
    return make


@pytest.fixture(autouse=True)
async def _clean_telegram_keys(redis):
    yield
    async for key in redis.scan_iter("flowforge:telegram:*"):
        await redis.delete(key)


def uid() -> int:
    return uuid.uuid4().int % 10**9


# --- Pure logic ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("nodes", "expected"), [
    ([{"id": "t", "type": "text", "config": {"text": "x"}}], False),
    ([{"id": "h", "type": "http_request", "config": {"url": "https://x.org"}}], False),
    ([{"id": "h", "type": "http_request", "config": {"url": "https://x.org", "method": "HEAD"}}], False),
    ([{"id": "h", "type": "http_request", "config": {"url": "https://x.org", "method": "post"}}], True),
    ([{"id": "g", "type": "gmail", "config": {}}], True),
    ([{"id": "t", "type": "telegram", "config": {}}], True),
    ([{"id": "d", "type": "discord_webhook", "config": {}}], True),
    ([{"id": "n", "type": "notion_create_page", "config": {}}], True),
    ([{"id": "n", "type": "notion_query_database", "config": {}}], False),
    ([{"id": "a", "type": "airtable_create_record", "config": {}}], True),
    ([{"id": "r", "type": "gmail_read", "config": {}}], False),
    ([{"id": "r", "type": "gmail_read", "config": {"mark_as_read": True}}], True),
    ([{"id": "k", "type": "kb_add_document", "config": {}}], True),
])
def test_side_effect_detection(nodes, expected):
    assert has_side_effects(WorkflowGraph.model_validate({"nodes": nodes, "edges": []})) is expected


def test_confirmation_tokens_are_signed_bound_to_the_chat_and_expire():
    expires = int(time.time()) + 300
    data = callback_data("c", "abc123def456", CHAT, expires)
    assert len(data.encode()) <= 64  # Telegram's callback_data limit
    assert verify_callback(data, CHAT) == ("c", "abc123def456")
    with pytest.raises(ConfirmationError, match="isn't valid in this chat"):
        verify_callback(data, OTHER_CHAT)
    with pytest.raises(ConfirmationError, match="isn't valid"):
        verify_callback(data.replace(":c", ":x").replace("c:", "x:", 1), CHAT)  # the action is signed too
    with pytest.raises(ConfirmationError, match="isn't valid"):
        verify_callback(data[:-2] + "AA", CHAT)
    with pytest.raises(ConfirmationError, match="expired"):
        verify_callback(data, CHAT, now=expires + 1)
    with pytest.raises(ConfirmationError, match="isn't one of mine"):
        verify_callback("hello", CHAT)


def _candidate(name, inputs, side_effects=False):
    from app.schemas.deployment import DeploymentInput

    return Candidate(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), name, f"{name} description", side_effects,
                     [DeploymentInput(node_id=n, label=n, name=n, type=t, required=r) for n, t, r in inputs])


def test_router_decisions_clear_ambiguous_missing_and_invented():
    digest = _candidate("Morning digest", [])
    jobs = _candidate("Job search", [("query", "text", True), ("city", "text", False)])
    photo = _candidate("Receipt", [("photo", "file", True)])
    cands = [digest, jobs, photo]
    clear = decide({"pipeline": 2, "inputs": {"query": "backend AI", "city": "Pune"}, "confidence": 0.9, "missing_inputs": []}, cands, False)
    assert clear.kind == "run" and clear.candidate is jobs and clear.inputs == {"query": "backend AI", "city": "Pune"}
    unsure = decide({"pipeline": 2, "inputs": {"query": "x"}, "confidence": 0.3, "missing_inputs": [], "question": "Jobs or digest?"}, cands, False)
    assert unsure.kind == "ask" and unsure.question == "Jobs or digest?"
    # The model claims it's fine, but the required input isn't there: still ask.
    missing = decide({"pipeline": 2, "inputs": {}, "confidence": 0.95, "missing_inputs": []}, cands, False)
    assert missing.kind == "ask" and "query" in missing.question
    # Inputs the pipeline doesn't have are dropped, never passed on.
    invented = decide({"pipeline": 1, "inputs": {"secret_flag": "yes"}, "confidence": 0.9, "missing_inputs": []}, cands, False)
    assert invented.kind == "run" and invented.inputs == {}
    no_file = decide({"pipeline": 3, "inputs": {}, "confidence": 0.9, "missing_inputs": []}, cands, False)
    assert no_file.kind == "ask" and "photo" in no_file.question
    assert decide({"pipeline": 3, "inputs": {}, "confidence": 0.9, "missing_inputs": []}, cands, True).kind == "run"
    nothing = decide({"pipeline": None, "inputs": {}, "confidence": 0, "missing_inputs": []}, cands, False)
    assert nothing.kind == "none"
    out_of_range = decide({"pipeline": 7, "inputs": {}, "confidence": 1, "missing_inputs": [], "question": "Which?"}, cands, False)
    assert out_of_range.kind == "ask"


# --- Through the command center -------------------------------------------------------------------


async def test_a_clear_message_runs_the_pipeline_and_replies_with_the_result(client, user, bot, make_center):
    wid, deployment = await connect(client, user, greeting_graph(), "Greets a person by name")
    llm = router({"pipeline": 1, "inputs": {"name": "Asha"}, "confidence": 0.95, "missing_inputs": []})
    center = make_center(llm)
    assert await center.handle_update(message(uid(), "say hi to Asha")) == "running"
    await center.drain()
    assert bot.texts() == ["⏳ Running Greets a person by name…", "✅ Greets a person by name\n\nHello, Asha!"]
    assert "Greets a person by name" in llm.prompts[0] and "name (text, required)" in llm.prompts[0]
    runs = (await client.get(f"/api/workflows/{wid}/executions", headers=user.headers)).json()
    assert runs[0]["trigger"] == "telegram" and runs[0]["status"] == "success"


async def test_each_update_is_handled_once_even_after_a_restart(client, user, bot, make_center):
    await connect(client, user, greeting_graph(), "Greets a person by name")
    reply = {"pipeline": 1, "inputs": {"name": "Asha"}, "confidence": 0.95, "missing_inputs": []}
    update = message(uid(), "hi Asha")
    first = make_center(router(reply))
    assert await first.handle_update(update) == "running"
    await first.drain()
    restarted = make_center(router(reply))  # a new listener process, same Redis
    assert await restarted.handle_update(update) == "duplicate"
    assert len([t for t in bot.texts() if t.startswith("✅")]) == 1


async def test_chats_off_the_allowlist_are_ignored_silently(client, user, bot, make_center):
    await connect(client, user, greeting_graph(), "Greets a person by name")
    center = make_center(router())  # the router must not even be asked
    assert await center.handle_update(message(uid(), "hi", chat=OTHER_CHAT)) == "not_allowed"
    assert bot.sent == []


async def test_an_ambiguous_message_gets_one_question_and_the_answer_is_routed_with_it(client, user, bot, make_center):
    await connect(client, user, greeting_graph(), "Greets a person by name")
    llm = router(
        {"pipeline": 1, "inputs": {}, "confidence": 0.9, "missing_inputs": ["name"], "question": "Who should I greet?"},
        {"pipeline": 1, "inputs": {"name": "Ravi"}, "confidence": 0.9, "missing_inputs": []},
    )
    center = make_center(llm)
    assert await center.handle_update(message(uid(), "greet someone")) == "asked"
    assert bot.texts() == ["Who should I greet?"]
    assert await center.handle_update(message(uid(), "Ravi")) == "running"
    await center.drain()
    assert "greet someone\nRavi" in llm.prompts[1]  # the answer is routed together with the question's message
    assert bot.texts()[-1].endswith("Hello, Ravi!")


async def test_still_unclear_after_one_question_lists_what_it_can_run(client, user, bot, make_center):
    await connect(client, user, greeting_graph(), "Greets a person by name")
    unsure = {"pipeline": None, "inputs": {}, "confidence": 0.2, "missing_inputs": [], "question": "What do you need?"}
    center = make_center(router(unsure, unsure))
    assert await center.handle_update(message(uid(), "do the thing")) == "asked"
    assert await center.handle_update(message(uid(), "the other thing")) == "no_match"
    assert "I can run:\n• Greets a person by name" in bot.texts()[-1]


async def test_a_side_effect_pipeline_waits_for_confirm(client, user, bot, make_center):
    await connect(client, user, email_graph(), "Emails a note to my manager")
    center = make_center(router({"pipeline": 1, "inputs": {"note": "Deploy is done"}, "confidence": 0.9, "missing_inputs": []}))
    assert await center.handle_update(message(uid(), "tell my manager the deploy is done")) == "confirming"
    prompt = bot.sent[-1]
    assert prompt["text"].startswith("Run “Emails a note to my manager”?") and "note: Deploy is done" in prompt["text"]
    assert MockEmailProvider.outbox() == []  # nothing ran yet
    confirm, cancel = (b["callback_data"] for b in prompt["reply_markup"]["inline_keyboard"][0])

    # A tap from another chat is refused, and doesn't use up the confirmation.
    assert await center.handle_update(tap(uid(), confirm, chat=OTHER_CHAT)) == "invalid"
    assert await center.handle_update(tap(uid(), confirm)) == "confirmed"
    await center.drain()
    [sent] = MockEmailProvider.outbox()
    assert sent.body == "Deploy is done"
    assert bot.edits[-1][2] == "✅ Confirmed: Emails a note to my manager"
    assert bot.texts()[-2:] == ["⏳ Running Emails a note to my manager…", "✅ Emails a note to my manager\n\nDone."]
    # Confirming twice (or Cancel afterwards) does nothing more.
    assert await center.handle_update(tap(uid(), confirm)) == "already_handled"
    assert await center.handle_update(tap(uid(), cancel)) == "already_handled"
    assert len(MockEmailProvider.outbox()) == 1


async def test_cancel_runs_nothing(client, user, bot, make_center):
    await connect(client, user, email_graph(), "Emails a note to my manager")
    center = make_center(router({"pipeline": 1, "inputs": {"note": "x"}, "confidence": 0.9, "missing_inputs": []}))
    await center.handle_update(message(uid(), "email my manager x"))
    cancel = bot.sent[-1]["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
    assert await center.handle_update(tap(uid(), cancel)) == "cancelled"
    assert MockEmailProvider.outbox() == [] and bot.edits[-1][2] == "✖ Cancelled; nothing was run."


async def test_an_expired_confirmation_is_rejected_with_a_clear_message(client, user, bot, make_center, monkeypatch):
    await connect(client, user, email_graph(), "Emails a note to my manager")
    center = make_center(router({"pipeline": 1, "inputs": {"note": "x"}, "confidence": 0.9, "missing_inputs": []}))
    await center.handle_update(message(uid(), "email my manager x"))
    confirm = bot.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    later = time.time() + settings.TELEGRAM_CONFIRM_SECONDS + 1
    monkeypatch.setattr("app.services.telegram_center.time.time", lambda: later)
    assert await center.handle_update(tap(uid(), confirm)) == "expired"
    assert "expired" in bot.answers[-1][1] and "Send the request again" in bot.answers[-1][1]
    assert bot.edits[-1][2].startswith("⌛ This confirmation expired")
    assert MockEmailProvider.outbox() == []


async def test_messages_with_secrets_or_ids_run_nothing(client, user, bot, make_center):
    await connect(client, user, greeting_graph(), "Greets a person by name")
    center = make_center(router())
    assert await center.handle_update(message(uid(), "greet card 4111 1111 1111 1111")) == "blocked"
    assert bot.texts() == ["I didn't run anything: your message contains 1 card number. Remove it and send the request again."]


async def test_replies_are_masked(client, user, bot, make_center):
    graph = greeting_graph()
    graph["nodes"][1]["config"]["text"] = "Your key is AKIAIOSFODNN7EXAMPLE, {{who.value}}"
    await connect(client, user, graph, "Greets a person by name")
    center = make_center(router({"pipeline": 1, "inputs": {"name": "Asha"}, "confidence": 0.9, "missing_inputs": []}))
    await center.handle_update(message(uid(), "hi Asha"))
    await center.drain()
    assert bot.texts()[-1].endswith("Your key is [REDACTED:AWS_ACCESS_KEY], Asha")


async def test_voice_notes_are_transcribed_then_routed(client, user, bot, make_center):
    await connect(client, user, greeting_graph(), "Greets a person by name")
    bot.files["voice1"] = (settings.samples_dir / "team-meeting.mp3").read_bytes()
    llm = router({"pipeline": 1, "inputs": {"name": "Asha"}, "confidence": 0.9, "missing_inputs": []})
    center = make_center(llm)
    update = message(uid(), voice={"file_id": "voice1", "duration": 3, "mime_type": "audio/ogg"})
    assert await center.handle_update(update) == "running"
    await center.drain()
    assert bot.texts()[0] == "🎙️ Transcribing your voice note…"
    assert bot.texts()[1].startswith("🎙️ I heard: [MOCK TRANSCRIPT")  # the mock Whisper in tests
    assert "Message: [MOCK TRANSCRIPT" in llm.prompts[0]


async def test_photos_fill_the_pipelines_file_input(client, user, bot, make_center):
    await connect(client, user, photo_graph(), "Reads a receipt photo")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    bot.files["p1"] = png
    center = make_center(router({"pipeline": 1, "inputs": {}, "confidence": 0.9, "missing_inputs": []}))
    update = message(uid(), caption="my receipt", photo=[{"file_id": "p0", "width": 90, "file_size": 10},
                                                         {"file_id": "p1", "width": 800, "file_size": 72}])
    assert await center.handle_update(update) == "running"
    await center.drain()
    assert bot.texts()[-1].endswith("photo.jpg")  # the largest size was downloaded and stored as an upload
    files = (await client.get("/api/files", headers=user.headers)).json()
    assert files[0]["content_type"] == "image/png"


async def test_a_calendar_file_in_the_output_is_sent_as_a_document(client, user, bot, make_center):
    graph = {
        "nodes": [
            {"id": "what", "type": "input", "config": {"name": "title"}},
            {"id": "ics", "type": "ics_calendar", "config": {"title": "{{what.value}}", "start": "2026-10-11T10:00",
                                                              "end": "2026-10-11T13:00"}},
            {"id": "out", "type": "output", "config": {"name": "invite", "value": {
                "message": "📅 {{what.value}} on {{ics.start}}", "file": "{{ics.attachment}}"}}},
        ],
        "edges": [{"source": "what", "target": "ics"}, {"source": "ics", "target": "out"}],
    }
    await connect(client, user, graph, "Makes a calendar invite")
    center = make_center(router({"pipeline": 1, "inputs": {"title": "GATE mock"}, "confidence": 0.9, "missing_inputs": []}))
    assert await center.handle_update(message(uid(), "invite for GATE mock")) == "running"
    await center.drain()
    sent = bot.sent[-1]
    assert sent["document"] == "GATE-mock.ics" and sent["content_type"] == "text/calendar"
    assert sent["data"].startswith(b"BEGIN:VCALENDAR")
    assert sent["text"] == "✅ Makes a calendar invite\n\n📅 GATE mock on 2026-10-11T10:00:00+05:30"


async def test_an_image_in_the_output_is_sent_as_a_photo(client, user, bot, make_center):
    import base64

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    graph = {
        "nodes": [
            {"id": "who", "type": "input", "config": {"name": "name"}},
            {"id": "out", "type": "output", "config": {"name": "copy", "value": {
                "message": "Here, {{who.value}}",
                "file": {"filename": "x-redacted.png", "encoding": "base64", "content_type": "image/png",
                         "content": base64.b64encode(png).decode()}}}},
        ],
        "edges": [{"source": "who", "target": "out"}],
    }
    await connect(client, user, graph, "Returns a picture")
    center = make_center(router({"pipeline": 1, "inputs": {"name": "Asha"}, "confidence": 0.9, "missing_inputs": []}))
    await center.handle_update(message(uid(), "picture for Asha"))
    await center.drain()
    sent = bot.sent[-1]
    assert sent["photo"] == "x-redacted.png" and sent["data"] == png and sent["text"].endswith("Here, Asha")


def test_split_attachments():
    from app.services.telegram_center import split_attachments

    rest, files = split_attachments({"invite": {"message": "hi", "file": {"filename": "a.ics", "content": "X"}}})
    assert rest == {"invite": {"message": "hi"}} and files == [{"filename": "a.ics", "content": "X"}]
    assert split_attachments({"answer": "plain"}) == ({"answer": "plain"}, [])


async def test_per_chat_rate_limit(client, user, bot, make_center, monkeypatch):
    await connect(client, user, greeting_graph(), "Greets a person by name")
    monkeypatch.setattr(settings, "TELEGRAM_RATE_LIMIT_PER_MINUTE", 2)
    reply = {"pipeline": 1, "inputs": {"name": "A"}, "confidence": 0.9, "missing_inputs": []}
    center = make_center(router(reply, reply, reply))
    outcomes = [await center.handle_update(message(uid(), "hi A")) for _ in range(4)]
    await center.drain()
    assert outcomes == ["running", "running", "rate_limited", "rate_limited"]
    assert sum("more than 2 requests in a minute" in t for t in bot.texts()) == 1


async def test_without_an_llm_a_single_simple_pipeline_still_runs(client, user, bot, make_center):
    await connect(client, user, greeting_graph(), "Greets a person by name")
    center = make_center(router("not json", "still not json"))
    assert await center.handle_update(message(uid(), "Asha")) == "running"
    await center.drain()
    assert bot.texts()[-1].endswith("Hello, Asha!")


async def test_the_telegram_trigger_needs_a_deployment_and_defaults_its_allowlist(client, user, monkeypatch, shared_session):
    wid = await create_workflow(client, user, greeting_graph())
    off = await client.put(f"/api/workflows/{wid}/triggers/telegram", json={"enabled": True, "config": {}}, headers=user.headers)
    assert off.status_code == 422 and "Deploy the pipeline first" in off.json()["detail"]
    await deploy(client, user, wid, expect=201)
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "123456")
    on = await client.put(f"/api/workflows/{wid}/triggers/telegram", json={"enabled": True, "config": {}}, headers=user.headers)
    assert on.status_code == 200, on.text
    telegram = next(t for t in on.json()["triggers"] if t["type"] == "telegram")
    assert telegram["enabled"] is True and telegram["config"]["allowed_chat_ids"] == ["123456"]
    # Stored, not only shown: a message from that chat is routed.
    from app.models.trigger import WorkflowTrigger
    from sqlalchemy import select
    row = await shared_session.scalar(select(WorkflowTrigger).where(WorkflowTrigger.id == uuid.UUID(telegram["id"])))
    assert row.config_json == {"allowed_chat_ids": ["123456"]}
    bad = await client.put(f"/api/workflows/{wid}/triggers/telegram",
                           json={"enabled": True, "config": {"allowed_chat_ids": ["@me"]}}, headers=user.headers)
    assert bad.status_code == 422 and "not a Telegram chat id" in bad.json()["detail"]
