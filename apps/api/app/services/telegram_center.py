"""Telegram Command Center: run deployed pipelines by messaging the bot.

The listener (app.telegram_listener) long-polls the server's bot (TELEGRAM_BOT_TOKEN) and
hands every update to `CommandCenter.handle_update`:

1. Each update_id is handled at most once (an atomic Redis SET NX), whatever restarts or
   redeliveries happen.
2. A message counts only if its chat is on the allowlist of some workflow's Telegram
   trigger, and that workflow is deployed; anything else is ignored without a reply (and
   logged with the chat id only).
3. Voice notes are transcribed (the Speech to Text node); photos and documents are stored
   as the owner's uploads, for the pipeline's file input. Text is checked by the privacy
   guard: a message holding a secret, a card number, or an ID number runs nothing.
4. The intent router (an LLM, with the candidates' names, descriptions, and inputs) picks a
   pipeline and fills its inputs from the message. When it isn't sure, or a required input
   is missing, the bot asks one clarifying question; the reply is routed together with the
   first message.
5. A pipeline with side effects (it sends or writes something) waits for a Confirm tap. The
   buttons carry a token signed with the server's secret, bound to the chat and an expiry 5
   minutes out; the request itself waits in Redis, encrypted, and can be confirmed once.
   The same signed buttons carry the Discord voice monitor's "record this meeting?" prompt
   (actions v/n, a pending record with kind "discord_voice"): the tap's answer is handed to the
   discord-bot service through Redis (app.services.discord_voice), since this listener is the only
   getUpdates consumer.
6. The bot says "Running <name>…", runs the deployment like its API endpoint would (recorded
   with trigger `telegram`), and replies with the result, or the error and the run id. Replies
   go through the privacy layer's masking. A per-chat rate limit keeps a loop from running
   away.
"""

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from flowforge_engine import GraphNode, NodeContext, WorkflowGraph, execute_node, queue_for_graph
from flowforge_engine.errors import ProviderError
from flowforge_engine.nodes.structured import check_reply, schema_request
from flowforge_engine.privacy import PrivacyPolicy, describe, mask, scan
from redis.asyncio import Redis
from sqlalchemy import select

from app.core.config import settings
from app.core.crypto import get_cipher
from app.db.session import SessionFactory
from app.models.deployment import Deployment
from app.models.enums import ExecutionTrigger, TriggerType
from app.models.execution import WorkflowExecution
from app.models.trigger import WorkflowTrigger
from app.models.user import User
from app.models.workflow import Workflow
from app.schemas.deployment import DeploymentInput
from app.services.credentials import build_execution_services
from app.services.deployments import describe_io, execution_events, wait_for_execution
from app.services.files import UploadRejected, import_bytes
from app.services.privacy import policy_for
from app.services.runs import TERMINAL_STATUSES, InvalidWorkflowGraph, create_execution, fail_unqueued, utcnow
from app.services.task_queue import EnqueueFailed, TaskQueue
from app.services.triggers import limit_problem

logger = logging.getLogger(__name__)

UPDATE_KEY = "flowforge:telegram:update:{}"
OFFSET_KEY = "flowforge:telegram:offset"
PENDING_KEY = "flowforge:telegram:pending:{}"
CLARIFY_KEY = "flowforge:telegram:clarify:{}"
RATE_KEY = "flowforge:telegram:rate:{}:{}"
# The answer to a Discord voice recording prompt, for the discord-bot service ("yes" or "no").
VOICE_DECISION_KEY = "flowforge:discord:decision:{}"
VOICE_DECISION_TTL_SECONDS = 600
# Buttons of the Discord voice monitor's prompts: pending-record kind -> {action: answer}.
VOICE_PROMPTS = {
    "discord_voice": {"v": "yes", "n": "no"},
    "discord_output": {"s": "summary", "t": "transcript", "b": "both", "x": "none"},
}
VOICE_ANSWERS = ("v", "n", "s", "t", "b")  # ("x" is also a voice answer, but verify_callback already allows it)
VOICE_ANSWER_TEXT = {
    "yes": "✅ Approved: joining the channel to record.",
    "no": "✖ Declined; nothing will be recorded.",
    "summary": "✅ Sending the summary.",
    "transcript": "✅ Sending the full transcript.",
    "both": "✅ Sending the summary and the full transcript.",
    "none": "✖ No notes; you have the audio.",
}
# Handled update ids are remembered this long (Telegram keeps undelivered updates 24 h).
UPDATE_TTL_SECONDS = 7 * 24 * 3600
CLARIFY_TTL_SECONDS = 10 * 60
MIN_CONFIDENCE = 0.6
MAX_REPLY_CHARS = 3500
FREE_LLMS = ("gemini", "groq", "openrouter")


# --- Candidates ------------------------------------------------------------------------------


@dataclass
class Candidate:
    """A deployed pipeline this chat may run."""

    deployment_id: uuid.UUID
    workflow_id: uuid.UUID
    trigger_id: uuid.UUID
    owner_id: uuid.UUID
    name: str
    description: str
    side_effects: bool
    inputs: list[DeploymentInput]

    @property
    def required(self) -> list[DeploymentInput]:
        return [i for i in self.inputs if i.required]

    @property
    def file_input(self) -> DeploymentInput | None:
        return next((i for i in self.inputs if i.type == "file"), None)


async def candidates_for(db: Any, chat_id: str) -> list[Candidate]:
    """Deployed pipelines whose enabled Telegram trigger allows `chat_id`, oldest first."""
    rows = await db.execute(
        select(WorkflowTrigger, Deployment)
        .join(Deployment, Deployment.workflow_id == WorkflowTrigger.workflow_id)
        .where(WorkflowTrigger.type == TriggerType.TELEGRAM, WorkflowTrigger.enabled.is_(True),
               Deployment.revoked_at.is_(None))
        .order_by(Deployment.created_at)
    )
    found = []
    for trigger, deployment in rows.all():
        allowed = [str(c) for c in (trigger.config_json or {}).get("allowed_chat_ids") or []]
        if not allowed and settings.TELEGRAM_CHAT_ID:  # saved before the default was filled in
            allowed = [str(settings.TELEGRAM_CHAT_ID).strip()]
        if chat_id not in allowed:
            continue
        inputs, _ = describe_io(WorkflowGraph.model_validate(deployment.graph_json))
        found.append(Candidate(
            deployment.id, deployment.workflow_id, trigger.id, deployment.owner_id, deployment.name,
            deployment.description or "", bool(deployment.side_effects), inputs,
        ))
    return found


# --- Confirmations -------------------------------------------------------------------------------


class ConfirmationError(Exception):
    """A Confirm/Cancel tap that can't be honoured; the message says why."""


def _signature(action: str, pending_id: str, chat_id: str, expires: int) -> str:
    message = f"{action}:{pending_id}:{chat_id}:{expires}".encode()
    digest = hmac.new(settings.JWT_SECRET.encode(), message, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def callback_data(action: str, pending_id: str, chat_id: str, expires: int) -> str:
    """"c:<id>:<expiry>:<signature>" (or "x:..." for Cancel; "v:" and "n:" for a voice recording's
    Yes and No): at most 64 bytes, as Telegram allows."""
    data = f"{action}:{pending_id}:{expires}:{_signature(action, pending_id, chat_id, expires)}"
    assert len(data.encode()) <= 64
    return data


def verify_callback(data: str, chat_id: str, now: float | None = None) -> tuple[str, str]:
    """(action, pending id) of a genuine, unexpired tap in `chat_id`; else ConfirmationError."""
    try:
        action, pending_id, expires_text, signature = data.split(":")
        expires = int(expires_text)
    except ValueError:
        raise ConfirmationError("That button isn't one of mine.") from None
    if action not in ("c", "x", *VOICE_ANSWERS) or not hmac.compare_digest(signature, _signature(action, pending_id, chat_id, expires)):
        # A forged token, or a tap in another chat than the one it was sent to.
        raise ConfirmationError("That confirmation isn't valid in this chat.")
    if (now if now is not None else time.time()) > expires:
        raise ConfirmationError(
            f"This confirmation expired (they last {settings.TELEGRAM_CONFIRM_SECONDS // 60} minutes). Send the request again."
        )
    return action, pending_id


# --- Intent router -------------------------------------------------------------------------------

ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pipeline", "inputs", "confidence", "missing_inputs"],
    "properties": {
        "pipeline": {"type": ["integer", "null"]},
        "inputs": {"type": "object"},
        "confidence": {"type": "number"},
        "missing_inputs": {"type": "array", "items": {"type": "string"}},
        "question": {"type": ["string", "null"]},
    },
}


@dataclass
class Decision:
    kind: str  # "run", "ask", or "none"
    candidate: Candidate | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    question: str | None = None


def router_prompt(message: str, candidates: list[Candidate], has_file: bool) -> str:
    listed = []
    for n, c in enumerate(candidates, start=1):
        fields = ", ".join(
            f"{i.name} ({i.type}{', required' if i.required else ', optional'})" for i in c.inputs
        ) or "none"
        listed.append(f"{n}. {c.name}: {c.description or '(no description)'}\n   inputs: {fields}")
    attachment = "The user also attached a file (it fills a file input).\n" if has_file else ""
    return (
        "You route a user's message to one of their automation pipelines and fill in its inputs.\n\n"
        f"Pipelines:\n{chr(10).join(listed)}\n\n{attachment}Message: {message}\n\n"
        "Pick the pipeline the message asks for (its number), or null if none fits. Fill an input only with "
        "what the message actually says; never invent a value. List required text/number/json inputs the "
        "message doesn't give in missing_inputs (file inputs are filled from the attachment). Give confidence "
        "0-1. If you're unsure which pipeline, or something is missing, write one short question to ask the "
        "user in `question`."
    )


def decide(data: dict[str, Any], candidates: list[Candidate], has_file: bool) -> Decision:
    """What to do with the router's (validated) reply. The checks don't trust the model: an
    unknown pipeline, low confidence, or a required input it didn't fill means asking."""
    index = data.get("pipeline")
    chosen = candidates[index - 1] if isinstance(index, int) and 1 <= index <= len(candidates) else None
    question = (data.get("question") or "").strip() or None
    if chosen is None:
        return Decision("ask" if question else "none", question=question)
    names = {i.name: i for i in chosen.inputs}
    inputs = {k: v for k, v in (data.get("inputs") or {}).items() if k in names and names[k].type != "file" and v not in (None, "")}
    missing = [i.name for i in chosen.required if i.type != "file" and i.name not in inputs]
    file_input = chosen.file_input
    if file_input is not None and file_input.required and not has_file:
        missing.append(file_input.name)
    confidence = float(data.get("confidence") or 0)
    if missing or confidence < MIN_CONFIDENCE:
        default = (
            f"To run {chosen.name} I need: {', '.join(missing)}. What should I use?" if missing
            else f"Do you mean {chosen.name}? Say a bit more about what you want."
        )
        return Decision("ask", chosen, inputs, question or default)
    return Decision("run", chosen, inputs)


LLMCall = Callable[[str, str], Awaitable[str]]


async def route(llm: LLMCall, message: str, candidates: list[Candidate], has_file: bool) -> Decision:
    """Ask the LLM (one retry when its JSON doesn't match the schema)."""
    request = schema_request(router_prompt(message, candidates, has_file), ROUTER_SCHEMA)
    problems: list[str] = []
    for _ in range(2):
        prompt = request if not problems else f"{request}\n\nYour previous reply was invalid: {'; '.join(problems[:5])}."
        reply = await llm("You are a precise router. You reply with JSON only.", prompt)
        data, problems = check_reply(reply, ROUTER_SCHEMA)
        if not problems:
            return decide(data, candidates, has_file)
    raise ProviderError("router", f"the router's reply didn't match the schema: {'; '.join(problems[:3])}")


# --- Formatting -------------------------------------------------------------------------------


def format_result(final_output: Any) -> str:
    """The run's final output as a chat message: a lone string as is, otherwise JSON."""
    value = final_output
    while isinstance(value, dict) and len(value) == 1:
        value = next(iter(value.values()))
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    if not text.strip():
        text = "(no output)"
    return text if len(text) <= MAX_REPLY_CHARS else text[:MAX_REPLY_CHARS] + "\n… (cut; the full result is on the run's page)"


def _is_attachment(value: Any) -> bool:
    return isinstance(value, dict) and {"filename", "content"} <= value.keys() and isinstance(value.get("content"), str)


def split_attachments(value: Any) -> tuple[Any, list[dict[str, Any]]]:
    """(the output without file attachments, the attachments): an ICS Calendar node's
    {{ics.attachment}} (or any {filename, content, encoding, content_type}) in a final output is
    sent as a Telegram document rather than printed."""
    found: list[dict[str, Any]] = []

    def walk(item: Any) -> Any:
        if _is_attachment(item):
            found.append(item)
            return None
        if isinstance(item, dict):
            kept = {k: walk(v) for k, v in item.items()}
            return {k: v for k, v in kept.items() if v is not None}
        if isinstance(item, list):
            return [w for w in (walk(v) for v in item) if w is not None]
        return item

    return walk(value), found


# --- The command center ----------------------------------------------------------------------


Bot = Any  # TelegramProvider (or a test double with the same methods)


class CommandCenter:
    def __init__(
        self, bot: Bot, *, session_factory: SessionFactory, redis: Redis, task_queue: TaskQueue,
        llm_factory: Callable[[Any], LLMCall] | None = None, wait_seconds: float | None = None,
    ):
        self.bot, self.sessions, self.redis, self.task_queue = bot, session_factory, redis, task_queue
        self._llm_factory = llm_factory or self._owner_llm
        self.wait_seconds = wait_seconds if wait_seconds is not None else settings.TELEGRAM_RUN_WAIT_SECONDS
        self.background: set[asyncio.Task[Any]] = set()

    # -- entry point ---------------------------------------------------------------------------

    async def first_time(self, update_id: int) -> bool:
        """True exactly once per update_id, across restarts and listeners."""
        return bool(await self.redis.set(UPDATE_KEY.format(update_id), "1", nx=True, ex=UPDATE_TTL_SECONDS))

    async def handle_update(self, update: dict[str, Any]) -> str:
        """Handle one update; returns what happened (for logs and tests)."""
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return "invalid"
        if not await self.first_time(update_id):
            return "duplicate"
        if "callback_query" in update:
            return await self._callback(update["callback_query"])
        message = update.get("message")
        if not isinstance(message, dict) or "chat" not in message:
            return "ignored"
        return await self._message(message)

    # -- messages ------------------------------------------------------------------------------

    async def _message(self, message: dict[str, Any]) -> str:
        chat_id = str(message["chat"]["id"])
        async with self.sessions() as db:
            candidates = await candidates_for(db, chat_id)
        if not candidates:
            logger.info("telegram message from a chat on no allowlist; ignored", extra={"chat_id": chat_id})
            return "not_allowed"
        if not await self._within_rate(chat_id):
            return "rate_limited"
        owner_id = candidates[0].owner_id
        try:
            text, file_id = await self._content(message, owner_id, chat_id)
        except _Reply as reply:
            await self._say(chat_id, str(reply))
            return "media_failed"
        if text is None and file_id is None:
            await self._say(chat_id, "Send me a message, a voice note, or a file, and I'll run the matching pipeline.")
            return "empty"
        if text and (findings := scan(text)):
            await self._say(
                chat_id,
                f"I didn't run anything: your message contains {describe(findings)}. Remove it and send the request again.",
            )
            logger.info("telegram message held sensitive data; not routed", extra={"chat_id": chat_id, "types": sorted({f.type for f in findings})})
            return "blocked"

        clarifying = await self._take_clarification(chat_id)
        if clarifying:
            text = f"{clarifying['text']}\n{text or ''}".strip()
            file_id = file_id or clarifying.get("file_id")
        try:
            decision = await route(self._llm_factory(await self._owner(owner_id)), text or "(a file, no text)", candidates, bool(file_id))
        except ProviderError as exc:
            decision = self._fallback(candidates, text, bool(file_id))
            if decision is None:
                await self._say(chat_id, f"I couldn't work out which pipeline to run ({exc}). Try again in a moment.")
                return "router_failed"
        if decision.kind != "run":
            if clarifying or decision.kind == "none":
                names = "\n".join(f"• {c.name}: {c.description}" for c in candidates)
                await self._say(chat_id, f"I'm not sure which pipeline you mean. I can run:\n{names}")
                return "no_match"
            await self._remember_clarification(chat_id, text or "", file_id)
            await self._say(chat_id, decision.question or "Could you say a bit more?")
            return "asked"

        assert decision.candidate is not None
        inputs = dict(decision.inputs)
        if file_id and decision.candidate.file_input is not None:
            inputs[decision.candidate.file_input.name] = file_id
        if decision.candidate.side_effects:
            await self._ask_confirmation(chat_id, decision.candidate, inputs)
            return "confirming"
        self._start(self._run(chat_id, decision.candidate, inputs))
        return "running"

    @staticmethod
    def _fallback(candidates: list[Candidate], text: str | None, has_file: bool) -> Decision | None:
        """Without an LLM: one pipeline with at most one required text input can still run."""
        if len(candidates) != 1:
            return None
        only = candidates[0]
        texts = [i for i in only.required if i.type != "file"]
        if len(texts) > 1 or (texts and not text) or (only.file_input and only.file_input.required and not has_file):
            return None
        return Decision("run", only, {texts[0].name: text} if texts else {})

    async def _content(self, message: dict[str, Any], owner_id: uuid.UUID, chat_id: str) -> tuple[str | None, str | None]:
        """(text, uploaded file id): a voice note becomes its transcript."""
        text = (message.get("text") or message.get("caption") or "").strip() or None
        media = message.get("voice") or message.get("audio")
        if media:
            await self._say(chat_id, "🎙️ Transcribing your voice note…")
            record_id = await self._download(media["file_id"], owner_id, media.get("file_name") or "voice-note.ogg")
            transcript = await self._transcribe(owner_id, record_id)
            await self._say(chat_id, f"🎙️ I heard: {transcript}")
            return (f"{text}\n{transcript}".strip() if text else transcript), None
        if message.get("photo"):
            largest = max(message["photo"], key=lambda p: p.get("file_size") or p.get("width", 0))
            return text, await self._download(largest["file_id"], owner_id, "photo.jpg")
        if message.get("document"):
            doc = message["document"]
            return text, await self._download(doc["file_id"], owner_id, doc.get("file_name") or "document")
        return text, None

    async def _download(self, telegram_file_id: str, owner_id: uuid.UUID, filename: str) -> str:
        try:
            data, _ = await self.bot.download_file(telegram_file_id)
        except ProviderError as exc:
            raise _Reply(f"I couldn't download that file: {exc}") from None
        async with self.sessions() as db:
            try:
                record = await import_bytes(db, owner_id, data, filename)
            except UploadRejected as exc:
                raise _Reply(f"I can't use that file: {exc}") from None
            await db.commit()
            return str(record.id)

    async def _transcribe(self, owner_id: uuid.UUID, file_id: str) -> str:
        async with self.sessions() as db:
            owner = await db.get(User, owner_id)
            services = await build_execution_services(db, owner)
            # Groq's Whisper with a key (the mock in tests), faster-whisper on the CPU without.
            provider = "groq" if services.has_credentials("groq") else "local"
            context = NodeContext(workflow_id="telegram", execution_id=str(uuid.uuid4()), services=services)
            result = await execute_node(
                GraphNode(id="voice", type="speech_to_text", config={"file": file_id, "provider": provider}),
                context, node_timeout=600,
            )
        if result.status.value != "success" or not (result.output or {}).get("text", "").strip():
            raise _Reply(f"I couldn't transcribe that voice note: {result.error or 'no speech found'}")
        return result.output["text"].strip()

    # -- clarification, rate limit ----------------------------------------------------------------

    async def _remember_clarification(self, chat_id: str, text: str, file_id: str | None) -> None:
        token = get_cipher().encrypt({"text": text, "file_id": file_id})
        await self.redis.set(CLARIFY_KEY.format(chat_id), token, ex=CLARIFY_TTL_SECONDS)

    async def _take_clarification(self, chat_id: str) -> dict[str, Any] | None:
        token = await self.redis.getdel(CLARIFY_KEY.format(chat_id))
        return get_cipher().decrypt(token) if token else None

    async def _within_rate(self, chat_id: str) -> bool:
        """At most TELEGRAM_RATE_LIMIT_PER_MINUTE requests a minute per chat; the first one
        over gets one "slow down" reply, the rest are dropped."""
        key = RATE_KEY.format(chat_id, int(time.time() // 60))
        count = await self.redis.incr(key)
        if count == 1:
            await self.redis.expire(key, 120)
        limit = settings.TELEGRAM_RATE_LIMIT_PER_MINUTE
        if count == limit + 1:
            await self._say(chat_id, f"That's more than {limit} requests in a minute; wait a moment and try again.")
        if count > limit:
            logger.warning("telegram chat rate limited", extra={"chat_id": chat_id})
            return False
        return True

    # -- confirmation ----------------------------------------------------------------------------

    async def _ask_confirmation(self, chat_id: str, candidate: Candidate, inputs: dict[str, Any]) -> None:
        pending_id = uuid.uuid4().hex[:12]
        expires = int(time.time()) + settings.TELEGRAM_CONFIRM_SECONDS
        record = {"chat_id": chat_id, "deployment_id": str(candidate.deployment_id), "inputs": inputs, "expires": expires}
        # Kept a day so a late tap gets "expired" rather than "unknown"; the expiry is in the token.
        await self.redis.set(PENDING_KEY.format(pending_id), get_cipher().encrypt(record), ex=24 * 3600)
        shown = mask(inputs, PrivacyPolicy(detect_personal_data=True))[0]
        details = "\n".join(f"• {k}: {str(v)[:200]}" for k, v in shown.items()) or "• (no inputs)"
        await self.bot.send_message(
            chat_id,
            f"Run “{candidate.name}”? It sends or changes something outside FlowForge.\n{candidate.description}\n\n"
            f"{details}\n\nConfirm within {settings.TELEGRAM_CONFIRM_SECONDS // 60} minutes.",
            reply_markup={"inline_keyboard": [[
                {"text": "✅ Confirm", "callback_data": callback_data("c", pending_id, chat_id, expires)},
                {"text": "✖ Cancel", "callback_data": callback_data("x", pending_id, chat_id, expires)},
            ]]},
        )

    async def _callback(self, query: dict[str, Any]) -> str:
        message = query.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        query_id, data = str(query.get("id")), str(query.get("data") or "")
        try:
            action, pending_id = verify_callback(data, chat_id)
        except ConfirmationError as exc:
            await self.bot.answer_callback_query(query_id, str(exc), alert=True)
            if "expired" in str(exc) and message.get("message_id"):
                with contextlib.suppress(ProviderError):
                    await self.bot.edit_message_text(chat_id, message["message_id"], f"⌛ {exc}")
            logger.info("telegram confirmation rejected", extra={"chat_id": chat_id, "reason": str(exc)})
            return "expired" if "expired" in str(exc) else "invalid"
        token = await self.redis.getdel(PENDING_KEY.format(pending_id))
        if not token:
            await self.bot.answer_callback_query(query_id, "Already handled.")
            return "already_handled"
        record = get_cipher().decrypt(token)
        if record.get("kind") in VOICE_PROMPTS:
            return await self._voice_callback(action, pending_id, record, query_id, chat_id, message.get("message_id"))
        if action not in ("c", "x"):
            await self.bot.answer_callback_query(query_id, "That button isn't for this request.", alert=True)
            return "invalid"
        if record["chat_id"] != chat_id:  # can't happen with a valid signature; belt and braces
            await self.bot.answer_callback_query(query_id, "That confirmation isn't valid in this chat.", alert=True)
            return "invalid"
        message_id = message.get("message_id")
        if action == "x":
            await self.bot.answer_callback_query(query_id, "Cancelled")
            if message_id:
                await self.bot.edit_message_text(chat_id, message_id, "✖ Cancelled; nothing was run.")
            return "cancelled"
        await self.bot.answer_callback_query(query_id, "Confirmed")
        async with self.sessions() as db:
            candidate = next((c for c in await candidates_for(db, chat_id) if str(c.deployment_id) == record["deployment_id"]), None)
        if candidate is None:
            if message_id:
                await self.bot.edit_message_text(chat_id, message_id, "That pipeline isn't available anymore (undeployed, or its Telegram trigger is off).")
            return "gone"
        if message_id:
            await self.bot.edit_message_text(chat_id, message_id, f"✅ Confirmed: {candidate.name}")
        self._start(self._run(chat_id, candidate, record["inputs"]))
        return "confirmed"

    async def _voice_callback(
        self, action: str, pending_id: str, record: dict[str, Any], query_id: str, chat_id: str, message_id: int | None
    ) -> str:
        """A tap on one of the Discord voice monitor's prompts (record or not; summary, transcript,
        or both): the bot service picks the answer up from Redis."""
        answer = VOICE_PROMPTS[record["kind"]].get(action)
        if answer is None or record["chat_id"] != chat_id:
            await self.bot.answer_callback_query(query_id, "That confirmation isn't valid in this chat.", alert=True)
            return "invalid"
        await self.redis.set(VOICE_DECISION_KEY.format(pending_id), answer, ex=VOICE_DECISION_TTL_SECONDS)
        await self.bot.answer_callback_query(query_id, VOICE_ANSWER_TEXT[answer].lstrip("✅✖ ").rstrip("."))
        if message_id:
            await self.bot.edit_message_text(chat_id, message_id, VOICE_ANSWER_TEXT[answer])
        return f"voice_{answer}"

    # -- running -------------------------------------------------------------------------------

    def _start(self, work: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(work)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def drain(self) -> None:
        """Wait for runs started in the background (tests, shutdown)."""
        while self.background:
            await asyncio.gather(*list(self.background), return_exceptions=True)

    async def _run(self, chat_id: str, candidate: Candidate, inputs: dict[str, Any]) -> str:
        await self._say(chat_id, f"⏳ Running {candidate.name}…")
        async with self.sessions() as db:
            deployment = await db.get(Deployment, candidate.deployment_id)
            workflow = await db.get(Workflow, candidate.workflow_id)
            owner = await db.get(User, candidate.owner_id)
            graph = WorkflowGraph.model_validate(deployment.graph_json)
            services = await build_execution_services(db, owner)
            now = utcnow()
            if problem := await limit_problem(db, workflow, now):
                await self._say(chat_id, f"⚠️ {candidate.name} didn't run: {problem}")
                return "limited"
            queue = queue_for_graph(graph)
            try:
                execution = await create_execution(
                    db, workflow, None, inputs, services, queue=queue, graph=graph, trigger=ExecutionTrigger.TELEGRAM,
                    deployment_id=deployment.id, trigger_id=candidate.trigger_id, created_at=now,
                )
            except InvalidWorkflowGraph as exc:
                await db.rollback()
                await self._say(chat_id, f"❌ {candidate.name} can't run: " + "; ".join(i.message for i in exc.issues[:3]))
                return "invalid"
            await db.commit()
            execution_id = execution.id
            policy = policy_for(workflow.privacy_json)
            async with execution_events(self.redis, execution_id) as events:
                try:
                    await self.task_queue.enqueue(execution_id, queue)
                except EnqueueFailed as exc:
                    await fail_unqueued(db, execution_id, exc)
                    await self._say(chat_id, f"❌ {candidate.name} couldn't start (the task queue is down). Run {execution_id}")
                    return "enqueue_failed"
                finished = await wait_for_execution(self.sessions, events, execution_id, self.wait_seconds)
        async with self.sessions() as db:
            execution = await db.get(WorkflowExecution, execution_id, populate_existing=True)
        if not finished or execution.status not in TERMINAL_STATUSES:
            await self._say(chat_id, f"⏳ {candidate.name} is still running; I'll stop waiting here. Run {execution_id}")
            return "still_running"
        if execution.status.value == "success":
            # Without Output nodes the final output is the last steps' raw results (an email's
            # message id, ...): say it's done instead.
            has_outputs = bool(describe_io(graph)[1])
            rest, files = split_attachments(execution.final_output_json)
            result = mask(format_result(rest), policy)[0] if has_outputs and rest else "Done."
            if files:
                await self._send_files(chat_id, f"✅ {candidate.name}\n\n{result}", files)
            else:
                await self._say(chat_id, f"✅ {candidate.name}\n\n{result}")
            return "success"
        error = mask(execution.error_message or execution.status.value, policy)[0]
        await self._say(chat_id, f"❌ {candidate.name} failed: {error}\nRun {execution_id}")
        return "failed"

    # -- helpers -------------------------------------------------------------------------------

    async def _say(self, chat_id: str, text: str) -> None:
        """Every reply goes out masked (secrets, cards, IDs), whatever produced it."""
        safe = mask(text, PrivacyPolicy())[0]
        try:
            await self.bot.send_message(chat_id, safe[:4096])
        except ProviderError as exc:
            logger.warning("telegram reply failed", extra={"chat_id": chat_id, "error": str(exc)})

    async def _send_files(self, chat_id: str, text: str, files: list[dict[str, Any]]) -> None:
        """The files as documents, the text as the first one's caption (or a message before
        them when it's over Telegram's 1,024-character caption limit)."""
        caption: str | None = mask(text, PrivacyPolicy())[0]
        if len(caption) > 1024:
            await self._say(chat_id, caption)
            caption = None
        for item in files:
            data = (base64.b64decode(item["content"]) if item.get("encoding") == "base64"
                    else str(item["content"]).encode("utf-8"))
            content_type = item.get("content_type") or "application/octet-stream"
            # Images as photos (they preview in the chat; up to 10 MB), everything else as a document.
            send = self.bot.send_photo if content_type.startswith("image/") and len(data) <= 10 * 1024 * 1024 else self.bot.send_document
            try:
                await send(chat_id, str(item["filename"]), data, content_type, caption=caption)
            except ProviderError as exc:
                await self._say(chat_id, f"{caption or ''}\n(I couldn't attach {item['filename']}: {exc})".strip())
            caption = None

    async def _owner(self, owner_id: uuid.UUID) -> User:
        async with self.sessions() as db:
            return await db.get(User, owner_id)

    def _owner_llm(self, owner: User) -> LLMCall:
        """The owner's free LLMs in order (Gemini, Groq, OpenRouter), each tried in turn."""

        async def call(system: str, prompt: str) -> str:
            async with self.sessions() as db:
                services = await build_execution_services(db, owner)
            names = ["mock"] if services.settings.testing else [n for n in FREE_LLMS if services.has_credentials(n)]
            errors = []
            for name in names:
                try:
                    return await services.llm(name).generate(system, prompt, services.default_model(name), 0.0, 1024)
                except ProviderError as exc:
                    errors.append(str(exc))
            raise ProviderError("router", "; ".join(errors) or "no LLM key is configured (Gemini, Groq, or OpenRouter)")

        return call


class _Reply(Exception):
    """Stop handling and tell the user this."""
