"""Discord voice monitoring: opt-in recording of a meeting, summarized into the Telegram chat.

The discord-bot service (app.discord_bot) watches one voice channel. This module holds the
parts that don't need Discord, so they can be tested on their own:

- `SessionTracker`: which humans are in the channel and what the current session's state is. A
  session starts when the channel goes from empty to non-empty and ends when it's empty again.
  Each session is asked about at most once (a declined or expired prompt isn't repeated while
  people keep talking; the next, separate session asks again).
- `request_consent`: the Telegram prompt with Yes/No buttons. It reuses the Command Center's
  signed callback tokens (bound to the chat, expiring after TELEGRAM_CONFIRM_SECONDS), and the
  telegram-listener, the only getUpdates consumer, hands the tap's answer over through Redis.
- `MixedRecorder`: everyone's voice mixed into one mono 16 kHz WAV (what Whisper wants).
- `process_recording`: stores the WAV and runs the Meeting Notes pipeline on it (trigger
  `discord_voice`). Its Telegram nodes are pointed at the chat that approved the recording; the
  bot says so in that chat when anything fails.
"""

import asyncio
import copy
import enum
import io
import json
import logging
import threading
import time
import uuid
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from flowforge_engine import WorkflowGraph, queue_for_graph
from flowforge_engine.errors import ProviderError
from flowforge_engine.privacy import PrivacyPolicy, mask
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.core.config import settings
from app.core.crypto import get_cipher
from app.db.session import SessionFactory
from app.models.deployment import Deployment
from app.models.enums import ExecutionTrigger, WorkflowStatus
from app.models.execution import WorkflowExecution
from app.models.user import User
from app.models.workflow import Workflow
from app.services.credentials import build_execution_services
from app.services.deployments import describe_io, execution_events, wait_for_execution
from app.services.files import UploadRejected, import_bytes
from app.services.privacy import policy_for
from app.services.runs import TERMINAL_STATUSES, InvalidWorkflowGraph, create_execution, fail_unqueued
from app.services.task_queue import EnqueueFailed, TaskQueue
from app.services.telegram_center import PENDING_KEY, VOICE_DECISION_KEY, callback_data

logger = logging.getLogger(__name__)

PENDING_TTL_SECONDS = 24 * 3600
SAMPLE_RATE = 16000
# Discord delivers 48 kHz stereo 16-bit PCM, in 20 ms frames.
DISCORD_RATE, DISCORD_CHANNELS = 48000, 2
# A user's audio that arrives this far after the end of their previous frame left a gap (silence).
GAP_TOLERANCE_SECONDS = 0.1
# A frame this far from where its timestamp says it should be means the stream restarted.
REANCHOR_SECONDS = 5


# --- Sessions ----------------------------------------------------------------------------------


class Phase(str, enum.Enum):
    IDLE = "idle"  # channel empty
    ASKING = "asking"  # Telegram prompt out, waiting for a tap
    DECLINED = "declined"  # this session was asked (declined or expired) or can't be asked; stay quiet
    RECORDING = "recording"
    DONE = "done"  # recorded (or capped); don't ask again this session


class Action(str, enum.Enum):
    NONE = "none"
    ASK = "ask"  # send the Telegram prompt
    STOP = "stop"  # the session ended: stop recording / drop the prompt


class SessionTracker:
    """Humans in the monitored channel and the current session's phase.

    `session` increments whenever a new session starts, so work started for an older session
    (a prompt waiting for a tap) can tell it's stale.
    """

    def __init__(self, present: set[int] | None = None):
        self.humans: set[int] = set(present or ())
        self.session = 0
        # People already there when the bot starts: an ongoing session it wasn't asked about.
        self.phase = Phase.DECLINED if self.humans else Phase.IDLE

    def joined(self, user_id: int) -> Action:
        was_empty = not self.humans
        self.humans.add(user_id)
        if was_empty and self.phase is Phase.IDLE:
            self.session += 1
            self.phase = Phase.ASKING
            return Action.ASK
        return Action.NONE

    def left(self, user_id: int) -> Action:
        self.humans.discard(user_id)
        if self.humans:
            return Action.NONE
        ended = self.phase in (Phase.ASKING, Phase.RECORDING)
        self.phase = Phase.IDLE
        return Action.STOP if ended else Action.NONE

    def approved(self, session: int) -> bool:
        """The prompt was answered Yes: True if recording should start (the session is still the
        one that was asked)."""
        if session != self.session or self.phase is not Phase.ASKING:
            return False
        self.phase = Phase.RECORDING
        return True

    def declined(self, session: int) -> None:
        """No, no answer in time, or a recording that couldn't start: this session isn't asked again."""
        if session == self.session and self.phase in (Phase.ASKING, Phase.RECORDING):
            self.phase = Phase.DECLINED

    def finished(self, session: int) -> None:
        """Recording stopped while people are still there (the maximum duration): don't ask again."""
        if session == self.session and self.phase is Phase.RECORDING:
            self.phase = Phase.DONE


# --- Telegram consent ----------------------------------------------------------------------------


def _mask(text: str) -> str:
    return mask(text, PrivacyPolicy())[0]


async def say(bot: Any, chat_id: str, text: str) -> None:
    """A plain Telegram message, masked like every Command Center reply; never raises."""
    try:
        await bot.send_message(chat_id, _mask(text)[:4096])
    except ProviderError as exc:
        logger.warning("telegram message failed", extra={"chat_id": chat_id, "error": str(exc)})


async def _ask(
    bot: Any, redis: Redis, chat_id: str, text: str, kind: str, buttons: list[tuple[str, str]], *,
    expired_text: str, now: Callable[[], float], sleep: Callable[[float], Awaitable[None]],
    poll_seconds: float, is_current: Callable[[], bool],
) -> str:
    """Send `text` with signed buttons ((label, action) pairs, answers from VOICE_PROMPTS[kind])
    and wait for the tap; returns the answer, or "expired" (no tap in time, or `is_current()`
    went False). The telegram-listener turns the tap into a Redis value."""
    pending_id = uuid.uuid4().hex[:12]
    expires = int(now()) + settings.TELEGRAM_CONFIRM_SECONDS
    record = {"kind": kind, "chat_id": chat_id, "expires": expires}
    await redis.set(PENDING_KEY.format(pending_id), get_cipher().encrypt(record), ex=PENDING_TTL_SECONDS)
    sent = await bot.send_message(
        chat_id, text,
        reply_markup={"inline_keyboard": [[
            {"text": label, "callback_data": callback_data(action, pending_id, chat_id, expires)} for label, action in buttons
        ]]},
    )
    answer: str | None = None
    try:
        while is_current() and now() <= expires + 2:
            value = await redis.get(VOICE_DECISION_KEY.format(pending_id))
            if value is not None:
                answer = value.decode() if isinstance(value, bytes) else str(value)
                break
            await sleep(poll_seconds)
    finally:
        if answer is None:
            # A later tap is refused by the signed token's expiry; the pending request is dropped.
            await redis.delete(PENDING_KEY.format(pending_id))
        await redis.delete(VOICE_DECISION_KEY.format(pending_id))
    if answer is None:
        message_id = (sent or {}).get("message_id")
        if message_id and is_current():
            try:
                await bot.edit_message_text(chat_id, message_id, expired_text)
            except ProviderError:
                pass
        return "expired"
    return answer


async def request_consent(
    bot: Any, redis: Redis, chat_id: str, channel_name: str, *,
    now: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    poll_seconds: float = 1.0, is_current: Callable[[], bool] = lambda: True,
) -> str:
    """Ask in `chat_id` whether to record; "yes", "no", or "expired"."""
    return await _ask(
        bot, redis, chat_id,
        f"Voice activity detected in #{_mask(channel_name)} — record and "
        f"{'summarize' if settings.DISCORD_OUTPUT == 'notes' else 'send you'} this meeting?\n\n"
        f"If you say yes, I'll join the channel, post a notice there that recording has started, and send "
        f"the {'summary' if settings.DISCORD_OUTPUT == 'notes' else 'audio recording'} here afterwards. "
        f"Answer within {settings.TELEGRAM_CONFIRM_SECONDS // 60} minutes.",
        "discord_voice", [("✅ Yes, record", "v"), ("✖ No", "n")],
        expired_text="⌛ No answer in time; nothing was recorded.", now=now, sleep=sleep,
        poll_seconds=poll_seconds, is_current=is_current,
    )


async def request_notes_offer(
    bot: Any, redis: Redis, chat_id: str, channel_name: str, *,
    now: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    poll_seconds: float = 1.0,
) -> str:
    """After the audio was sent: offer notes for it. "summary", "transcript", "both", "none"
    (declined), or "expired"."""
    answer = await _ask(
        bot, redis, chat_id,
        f"Want notes for this recording of #{_mask(channel_name)}? I can send the summary (decisions and "
        f"action items), the full transcript of the whole meeting, or both. Answer within "
        f"{settings.TELEGRAM_CONFIRM_SECONDS // 60} minutes.",
        "discord_output", [("📝 Summary", "s"), ("📄 Full transcript", "t"), ("Both", "b"), ("✖ No thanks", "x")],
        expired_text="⌛ No answer; you have the audio.", now=now, sleep=sleep, poll_seconds=poll_seconds,
        is_current=lambda: True,
    )
    return answer if answer in ("summary", "transcript", "both", "none") else "expired"


async def request_output_choice(
    bot: Any, redis: Redis, chat_id: str, channel_name: str, minutes: float, *,
    now: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    poll_seconds: float = 1.0,
) -> str:
    """Ask what to send for a finished recording: "summary", "transcript", "both"; without a
    tap in time, "summary"."""
    answer = await _ask(
        bot, redis, chat_id,
        f"Recording of #{_mask(channel_name)} finished ({minutes:.1f} min). What do you want: the summary "
        f"(decisions and action items) or the full transcript of the whole meeting?\n\nI'm preparing both now. "
        f"No answer in {settings.TELEGRAM_CONFIRM_SECONDS // 60} minutes: I'll send the summary.",
        "discord_output", [("📝 Summary", "s"), ("📄 Full transcript", "t"), ("Both", "b")],
        expired_text="⌛ No answer; sending the summary.", now=now, sleep=sleep, poll_seconds=poll_seconds,
        is_current=lambda: True,
    )
    return answer if answer in ("summary", "transcript", "both") else "summary"


# --- Recording -----------------------------------------------------------------------------------


# Runs of exact silence up to this long inside speech are lost frames, not pauses (Discord sends
# nothing at all while a speaker is quiet, so real pauses are longer).
MAX_CONCEALED_GAP = int(0.12 * SAMPLE_RATE)
FADE_SAMPLES = int(0.008 * SAMPLE_RATE)
TARGET_PEAK = 0.85 * 32767
MAX_GAIN = 6.0


def smooth(data: np.ndarray) -> np.ndarray:
    """Makes a mixed recording easier on the ear: short holes left by lost frames are bridged with
    a linear cross-fade (no clicks), every stretch of speech fades in and out over 8 ms, and the
    level is raised so the loudest peak reaches about -1.4 dBFS (at most 6x)."""
    out = data.astype(np.float32)
    silent = np.concatenate(([False], data == 0, [False]))
    edges = np.flatnonzero(silent[1:] != silent[:-1])  # alternating starts and ends of zero runs
    starts, ends = edges[0::2], edges[1::2]
    for start, end in zip(starts, ends, strict=False):
        if 0 < start and end < len(data) and end - start <= MAX_CONCEALED_GAP:
            out[start:end] = np.linspace(out[start - 1], out[end], end - start + 2, dtype=np.float32)[1:-1]
    voiced = np.concatenate(([False], out != 0, [False]))
    edges = np.flatnonzero(voiced[1:] != voiced[:-1])
    for start, end in zip(edges[0::2], edges[1::2], strict=False):
        n = min(FADE_SAMPLES, (end - start) // 2)
        if n > 1:
            ramp = np.linspace(0, 1, n, dtype=np.float32)
            out[start:start + n] *= ramp
            out[end - n:end] *= ramp[::-1]
    peak = float(np.abs(out).max()) if len(out) else 0.0
    if peak > 0:
        out *= min(MAX_GAIN, TARGET_PEAK / peak)
    return np.clip(out, -32768, 32767).astype(np.int16)


class MixedRecorder:
    """Mixes every speaker into one mono 16 kHz track on a shared timeline.

    `add` takes Discord's decoded PCM (48 kHz, stereo, 16-bit) as it arrives. Each speaker's
    frames are laid end to end; when a speaker was silent for a while (no frames), their next
    frame is placed at the wall-clock time it arrived, so the gaps stay silent and speakers
    overlap as they did. Samples are summed and clipped.
    """

    CHUNK = SAMPLE_RATE * 60

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._buffer = np.zeros(0, dtype=np.int16)
        self._cursor: dict[int, int] = {}
        self._anchor: dict[int, tuple[int, int]] = {}
        self._start: float | None = None
        self._stop: float | None = None
        self.frames = 0

    def start(self) -> None:
        self._start = self._clock()

    def stop(self) -> None:
        if self._stop is None:
            self._stop = self._clock()

    @property
    def seconds(self) -> float:
        """Wall-clock length of the recording (start to stop, or to now while it runs)."""
        if self._start is None:
            return 0.0
        return (self._stop if self._stop is not None else self._clock()) - self._start

    def add(self, user_id: int, pcm: bytes, timestamp: int | None = None) -> None:
        """One decoded frame of `user_id`. With the frame's RTP `timestamp` (48 kHz ticks, 32-bit) it is
        placed exactly where it was spoken, so frames lost on the way leave a short gap instead of
        pulling the rest of the speech forward; without one, frames are laid end to end."""
        usable = len(pcm) // (2 * DISCORD_CHANNELS * 3) * (2 * DISCORD_CHANNELS * 3)
        if usable == 0:
            return
        stereo = np.frombuffer(pcm[:usable], dtype="<i2").reshape(-1, DISCORD_CHANNELS)
        mono = stereo.astype(np.int32).mean(axis=1)
        # 48 kHz -> 16 kHz: the mean of every three samples (a crude low-pass, plenty for speech).
        samples = mono.reshape(-1, DISCORD_RATE // SAMPLE_RATE).mean(axis=1).astype(np.int32)
        with self._lock:
            if self._start is None:
                self._start = self._clock()
            arrived = int((self._clock() - self._start) * SAMPLE_RATE)
            nominal = max(arrived - len(samples), 0)
            cursor = self._cursor.get(user_id)
            if timestamp is not None:
                anchor = self._anchor.get(user_id)
                if anchor is not None:
                    ticks = (timestamp - anchor[0] + 2**31) % 2**32 - 2**31  # signed, across the 32-bit wrap
                    position = anchor[1] + ticks // (DISCORD_RATE // SAMPLE_RATE)
                    if abs(position - nominal) > REANCHOR_SECONDS * SAMPLE_RATE:
                        anchor = None  # the stream restarted (new ssrc or timestamp base)
                if anchor is None:
                    anchor = self._anchor[user_id] = (timestamp, nominal)
                    position = nominal
                if position < 0:
                    return
                cursor = position
            elif cursor is None or nominal - cursor > GAP_TOLERANCE_SECONDS * SAMPLE_RATE:
                cursor = nominal
            end = cursor + len(samples)
            if end > len(self._buffer):
                grown = np.zeros(((end // self.CHUNK) + 1) * self.CHUNK, dtype=np.int16)
                grown[: len(self._buffer)] = self._buffer
                self._buffer = grown
            mixed = self._buffer[cursor:end].astype(np.int32) + samples
            self._buffer[cursor:end] = np.clip(mixed, -32768, 32767).astype(np.int16)
            self._cursor[user_id] = max(end, self._cursor.get(user_id, 0)) if timestamp is not None else end
            self.frames += 1

    def has_audio(self) -> bool:
        with self._lock:
            return bool(self._cursor) and bool(np.any(self._buffer))

    def to_wav(self, *, clean: bool = True) -> bytes:
        """The recording as a WAV file, padded with silence to its wall-clock length (and made
        smoother by `smooth`, unless `clean` is off)."""
        with self._lock:
            captured = max(self._cursor.values(), default=0)
            total = max(captured, int(self.seconds * SAMPLE_RATE))
            data = np.zeros(total, dtype=np.int16)
            data[:captured] = self._buffer[:captured]
        if clean:
            data = smooth(data)
        out = io.BytesIO()
        with wave.open(out, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(data.tobytes())
        return out.getvalue()


# --- Handoff to the Meeting Notes pipeline -----------------------------------------------------


@dataclass
class Pipeline:
    workflow: Workflow
    owner: User
    deployment: Deployment | None
    graph: dict[str, Any]


async def find_pipeline(db: Any, name: str) -> Pipeline | None:
    """The deployed pipeline called `name` (case-insensitive), else the workflow of that name
    (the newest first); None if neither exists."""
    deployment = await db.scalar(
        select(Deployment).where(func.lower(Deployment.name) == name.strip().lower(), Deployment.revoked_at.is_(None))
        .order_by(Deployment.deployed_at.desc()).limit(1)
    )
    if deployment is not None:
        workflow = await db.get(Workflow, deployment.workflow_id)
        graph = deployment.graph_json
    else:
        workflow = await db.scalar(
            select(Workflow).where(func.lower(Workflow.name) == name.strip().lower(), Workflow.status != WorkflowStatus.ARCHIVED)
            .order_by(Workflow.updated_at.desc()).limit(1)
        )
        graph = workflow.graph_json if workflow else {}
    if workflow is None:
        return None
    owner = await db.get(User, workflow.owner_id)
    return Pipeline(workflow, owner, deployment, graph)


def mute_telegram(graph: dict[str, Any]) -> dict[str, Any]:
    """A copy of `graph` whose Telegram nodes send nothing (mock): the bot delivers the result
    itself, in the form that was asked for."""
    graph = copy.deepcopy(graph)
    for node in graph.get("nodes", []):
        if node.get("type") == "telegram":
            node.setdefault("config", {})["auth"] = "mock"
    return graph


def find_transcript(final_output: Any) -> str | None:
    """The `transcript` text anywhere in a run's final output."""
    if isinstance(final_output, dict):
        if isinstance(final_output.get("transcript"), str):
            return final_output["transcript"]
        for value in final_output.values():
            if (found := find_transcript(value)) is not None:
                return found
    return None


def summary_text(final_output: Any) -> str:
    """A chat message from a run's final output (for pipelines that don't send it themselves)."""
    value = final_output
    notes = None
    while isinstance(value, dict):
        if isinstance(value.get("summary"), str):
            notes = value
            break
        if isinstance(value.get("notes"), dict):
            value = value["notes"]
        elif len(value) == 1:
            value = next(iter(value.values()))
        else:
            break
    if notes is not None:
        decisions = "\n".join(f"- {d}" for d in notes.get("decisions") or []) or "- (none)"
        actions = "\n".join(
            f"- {a.get('task')} (owner: {a.get('owner')}, due: {a.get('due')})" for a in notes.get("action_items") or []
        ) or "- (none)"
        return f"📝 Meeting notes\n\n{notes['summary']}\n\nDecisions\n{decisions}\n\nAction items\n{actions}"
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)[:3500]


async def deliver(
    bot: Any, chat_id: str, choice: str, final_output: Any, channel_name: str, seconds: float
) -> None:
    """Send what was asked for: the summary, the whole transcript (a .txt document), or both."""
    transcript = find_transcript(final_output)
    if choice in ("summary", "both") or transcript is None:
        await say(bot, chat_id, summary_text(final_output))
    if choice in ("transcript", "both"):
        if transcript is None:
            await say(bot, chat_id, "The pipeline's output has no transcript, so I could only send the summary.")
            return
        stamp = time.strftime("%Y-%m-%d", time.gmtime())
        data = _mask(transcript).encode("utf-8")
        try:
            await bot.send_document(
                chat_id, f"transcript-{channel_name}-{stamp}.txt", data, "text/plain",
                caption=f"📄 Full transcript of #{channel_name} ({seconds / 60:.1f} min)",
            )
        except ProviderError as exc:
            await say(bot, chat_id, f"I couldn't attach the transcript: {exc}")


TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024  # what a bot may upload


async def to_mp3(wav: bytes) -> bytes | None:
    """The recording as a 64 kbps MP3 (about 0.5 MB a minute, so 90 minutes fit Telegram's 50 MB
    limit; the WAV is 1.9 MB a minute), or None if ffmpeg can't."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-f", "wav", "-i", "pipe:0", "-codec:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate(wav)
    except OSError:
        return None
    if process.returncode != 0 or not out:
        logger.warning("ffmpeg could not encode the recording", extra={"error": err.decode(errors="replace")[:300]})
        return None
    return out


def clean_wav(wav: bytes) -> bytes:
    """A mono 16-bit WAV file made smoother by `smooth` (holes bridged, edges faded, level raised)."""
    with wave.open(io.BytesIO(wav)) as source:
        rate, frames = source.getframerate(), source.readframes(source.getnframes())
    data = smooth(np.frombuffer(frames, dtype="<i2"))
    out = io.BytesIO()
    with wave.open(out, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(data.tobytes())
    return out.getvalue()


async def send_recording(
    wav: bytes, seconds: float, *, chat_id: str, channel_name: str, bot: Any, session_factory: SessionFactory,
    clean: bool = False,
) -> str:
    """Send the recording itself to `chat_id` as an audio file (no transcript, no summary);
    returns "too_short", "too_large", "failed", or "sent". A copy is kept among the pipeline
    owner's uploads when there is one."""
    if seconds < settings.DISCORD_MIN_RECORDING_SECONDS:
        await say(bot, chat_id, f"The recording of #{channel_name} was only {seconds:.0f} seconds, so I didn't send it.")
        return "too_short"
    stamp = time.strftime("%Y-%m-%d-%H%M", time.gmtime())
    stored = ""
    if clean:  # a recording the discord-recorder mixed: smooth it here (numpy is in this image)
        wav = await asyncio.to_thread(clean_wav, wav)
    async with session_factory() as db:
        pipeline = await find_pipeline(db, settings.DISCORD_MEETING_PIPELINE)
        if pipeline is not None:
            try:
                await import_bytes(db, pipeline.owner.id, wav, f"discord-{channel_name}-{stamp}.wav")
                await db.commit()
                stored = " A copy is in your uploads."
            except UploadRejected as exc:
                logger.warning("recording not stored", extra={"error": str(exc)})
    mp3 = await to_mp3(wav)
    data, name, content_type = (mp3, f"{channel_name}-{stamp}.mp3", "audio/mpeg") if mp3 else (wav, f"{channel_name}-{stamp}.wav", "audio/wav")
    if len(data) > TELEGRAM_FILE_LIMIT:
        await say(bot, chat_id, f"I recorded #{channel_name} ({seconds / 60:.1f} min) but the file is {len(data) / 1048576:.0f} MB, "
                                f"over Telegram's 50 MB limit.{stored}")
        return "too_large"
    try:
        await bot.send_document(chat_id, name, data, content_type,
                                caption=f"🎙️ Recording of #{channel_name} ({seconds / 60:.1f} min)")
    except ProviderError as exc:
        await say(bot, chat_id, f"I recorded #{channel_name} but couldn't send the file: {exc}.{stored}")
        return "failed"
    return "sent"


async def process_recording(
    wav: bytes, seconds: float, *, chat_id: str, channel_name: str, bot: Any,
    session_factory: SessionFactory, redis: Redis, task_queue: TaskQueue, wait_seconds: float | None = None,
    choice_poll_seconds: float = 1.0, choice: str | None = None,
) -> str:
    """Summarize a finished recording for `chat_id`; returns what happened:
    "too_short", "no_pipeline", "rejected", "invalid", "enqueue_failed", "still_running",
    "failed", or "success".

    The Meeting Notes pipeline runs right away while the chat is asked what it wants (summary,
    full transcript, or both); the answer decides what is sent when the run finishes. With `choice`
    already made (the notes were asked for after the audio was sent) nothing more is asked."""
    if seconds < settings.DISCORD_MIN_RECORDING_SECONDS:
        await say(bot, chat_id, f"The recording of #{channel_name} was only {seconds:.0f} seconds, so I skipped the summary.")
        return "too_short"
    wait = wait_seconds if wait_seconds is not None else settings.DISCORD_RUN_WAIT_SECONDS
    async with session_factory() as db:
        pipeline = await find_pipeline(db, settings.DISCORD_MEETING_PIPELINE)
        if pipeline is None:
            await say(bot, chat_id, f"I recorded #{channel_name} but there is no “{settings.DISCORD_MEETING_PIPELINE}” "
                                    "pipeline to summarize it (create it from the Meeting Notes template).")
            return "no_pipeline"
        graph = mute_telegram(pipeline.graph)
        model = WorkflowGraph.model_validate(graph)
        file_input = next((i for i in describe_io(model)[0] if i.type == "file"), None)
        if file_input is None:
            await say(bot, chat_id, f"The “{settings.DISCORD_MEETING_PIPELINE}” pipeline has no file input for a recording.")
            return "invalid"
        stamp = time.strftime("%Y-%m-%d-%H%M", time.gmtime())
        try:
            record = await import_bytes(db, pipeline.owner.id, wav, f"discord-{channel_name}-{stamp}.wav")
        except UploadRejected as exc:
            await say(bot, chat_id, f"I recorded #{channel_name} but couldn't store the recording: {exc}")
            return "rejected"
        services = await build_execution_services(db, pipeline.owner)
        queue = queue_for_graph(model)
        try:
            execution = await create_execution(
                db, pipeline.workflow, None, {file_input.name: str(record.id)}, services, queue=queue, graph=model,
                trigger=ExecutionTrigger.DISCORD_VOICE,
                deployment_id=pipeline.deployment.id if pipeline.deployment else None,
            )
        except InvalidWorkflowGraph as exc:
            await db.rollback()
            await say(bot, chat_id, "I recorded the meeting but the pipeline can't run: " + "; ".join(i.message for i in exc.issues[:3]))
            return "invalid"
        await db.commit()
        execution_id = execution.id
        policy_json = pipeline.workflow.privacy_json
        choice_task = asyncio.ensure_future(request_output_choice(
            bot, redis, chat_id, channel_name, seconds / 60, poll_seconds=choice_poll_seconds,
        )) if choice is None else None
        async with execution_events(redis, execution_id) as events:
            try:
                await task_queue.enqueue(execution_id, queue)
            except EnqueueFailed as exc:
                if choice_task:
                    choice_task.cancel()
                await fail_unqueued(db, execution_id, exc)
                await say(bot, chat_id, f"❌ The meeting summary couldn't start (the task queue is down). Run {execution_id}")
                return "enqueue_failed"
            finished = await wait_for_execution(session_factory, events, execution_id, wait)
    async with session_factory() as db:
        execution = await db.get(WorkflowExecution, execution_id, populate_existing=True)
    if not finished or execution.status not in TERMINAL_STATUSES:
        if choice_task:
            choice_task.cancel()
        await say(bot, chat_id, f"⏳ The meeting summary is still running; I'll stop waiting here. Run {execution_id}")
        return "still_running"
    if execution.status.value != "success":
        if choice_task:
            choice_task.cancel()
        error = mask(execution.error_message or execution.status.value, policy_for(policy_json))[0]
        await say(bot, chat_id, f"❌ The meeting summary failed: {error}\nRun {execution_id}")
        return "failed"
    choice = choice or await choice_task
    await deliver(bot, chat_id, choice, execution.final_output_json, channel_name, seconds)
    return "success"
