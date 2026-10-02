"""The Discord side of the voice monitor when the discord-recorder service (discord.js) records.

That service reports what happens in the monitored voice channel and records when told to; this
controller decides. It reads the recorder's events from Redis, keeps the session state
(app.services.discord_voice.SessionTracker), asks in Telegram (a Yes/No the telegram-listener
answers), tells the recorder to start, and sends the finished recording to the same chat.

    flowforge:discord:events    recorder -> controller (see apps/discord-recorder/src/index.js)
    flowforge:discord:commands  controller -> recorder: start_recording, stop_recording

The recorder joins the channel only after a start_recording command, which this controller sends
only after a Yes tapped within the expiry.
"""

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from redis.asyncio import Redis

from app.core.config import settings
from app.services.discord_voice import (
    Action,
    Phase,
    SessionTracker,
    clean_wav,
    process_recording,
    request_consent,
    request_notes_offer,
    say,
    send_recording,
)
from app.services.task_queue import CeleryTaskQueue

logger = logging.getLogger(__name__)

EVENTS_KEY = "flowforge:discord:events"
COMMANDS_KEY = "flowforge:discord:commands"
# Joins and leaves older than this (the controller was down) are not acted on.
STALE_EVENT_SECONDS = 120
# Less speech than this in a recording and a summary isn't attempted.
MIN_SPEECH_SECONDS = 3


def recordings_dir() -> Path:
    return Path(settings.FILES_DIR) / "recordings"


class Controller:
    def __init__(self, telegram: Any, redis: Redis, sessions: Any, chat_id: str):
        self.telegram, self.redis, self.sessions, self.chat_id = telegram, redis, sessions, chat_id
        self.tracker = SessionTracker()
        self.channel_name = ""
        self.tasks: set[asyncio.Task[Any]] = set()
        self.task_queue = CeleryTaskQueue()

    # -- plumbing ------------------------------------------------------------------------------

    async def command(self, **command: Any) -> None:
        await self.redis.rpush(COMMANDS_KEY, json.dumps(command))

    def _spawn(self, work: Any) -> None:
        task = asyncio.ensure_future(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def drain(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    # -- events --------------------------------------------------------------------------------

    async def handle(self, event: dict[str, Any]) -> str:
        """Act on one recorder event; returns what happened (for logs and tests)."""
        kind = event.get("type")
        if event.get("channel_name"):
            self.channel_name = str(event["channel_name"])
        if kind == "snapshot":
            self.tracker = SessionTracker({int(u) for u in event.get("user_ids", [])})
            return "snapshot"
        if kind in ("member_joined", "member_left"):
            if time.time() - float(event.get("ts", 0)) / 1000 > STALE_EVENT_SECONDS:
                return "stale"
            user = int(event["user_id"])
            action = self.tracker.joined(user) if kind == "member_joined" else self.tracker.left(user)
            if action is Action.ASK:
                self._spawn(self._ask(self.tracker.session))
                return "asking"
            if action is Action.STOP:
                await self.command(type="stop_recording", reason="the channel is empty")
                return "stopping"
            return "none"
        if kind == "recording_started":
            return "recording"
        if kind == "recording_failed":
            self.tracker.declined(int(event.get("session", 0)))
            await say(self.telegram, self.chat_id,
                      f"I couldn't start recording #{self.channel_name} ({event.get('reason')}). "
                      "Check that the bot has Connect, Speak, and Send Messages there. Nothing was recorded.")
            return "failed"
        if kind == "recording_ready":
            return await self._deliver(event)
        return "ignored"

    async def _ask(self, session: int) -> None:
        try:
            answer = await request_consent(
                self.telegram, self.redis, self.chat_id, self.channel_name, is_current=lambda: self.tracker.session == session and self.tracker.phase is Phase.ASKING,
            )
        except Exception:
            logger.exception("the Telegram prompt failed")
            self.tracker.declined(session)
            return
        if answer != "yes":
            self.tracker.declined(session)
            logger.info("recording not approved", extra={"answer": answer})
            return
        if not self.tracker.approved(session):
            logger.info("approval arrived after the session ended; not recording")
            return
        await self.command(
            type="start_recording", session=session, max_minutes=settings.DISCORD_RECORDING_MAX_MINUTES,
            output=settings.DISCORD_OUTPUT,
        )

    async def _deliver(self, event: dict[str, Any]) -> str:
        """Send a finished recording to the chat that approved it (audio file, or the notes)."""
        session = int(event.get("session", 0))
        if event.get("reason") == "max_duration":
            self.tracker.finished(session)
        seconds, speech = float(event.get("seconds", 0)), float(event.get("speech_seconds", 0))
        name = str(event.get("channel_name") or self.channel_name)
        logger.info("recording finished", extra={k: event.get(k) for k in ("reason", "seconds", "speech_seconds", "frames", "decode_errors")})
        path = Path(str(event.get("path", ""))).resolve()
        if recordings_dir().resolve() not in path.parents or not path.is_file():
            logger.error("recording file is missing or outside the recordings directory", extra={"path": str(path)})
            await say(self.telegram, self.chat_id, f"I recorded #{name} but the recording file is missing.")
            return "missing"
        wav = path.read_bytes()
        try:
            notes = settings.DISCORD_OUTPUT == "notes"
            if seconds >= settings.DISCORD_MIN_RECORDING_SECONDS and (
                not event.get("has_audio") or (notes and speech < MIN_SPEECH_SECONDS)
            ):
                await say(self.telegram, self.chat_id,
                          f"I recorded #{name} for {seconds / 60:.1f} minutes but heard only {speech:.0f} seconds of speech, "
                          "so there is nothing to send.")
                return "no_audio"
            if notes:
                return await process_recording(
                    wav, seconds, chat_id=self.chat_id, channel_name=name, bot=self.telegram,
                    session_factory=self.sessions, redis=self.redis, task_queue=self.task_queue,
                )
            wav = await asyncio.to_thread(clean_wav, wav)  # the recorder's mix, smoothed
            outcome = await send_recording(
                wav, seconds, chat_id=self.chat_id, channel_name=name, bot=self.telegram, session_factory=self.sessions,
            )
            if outcome == "sent":  # then offer notes for it; the tap may come minutes later
                self._spawn(self._offer_notes(wav, seconds, name))
            return outcome
        finally:
            with contextlib.suppress(OSError):
                path.unlink()

    async def _offer_notes(self, wav: bytes, seconds: float, name: str) -> str:
        """Ask whether the chat wants the summary and/or the transcript of what it was just sent,
        and produce them from the same recording only if it does."""
        try:
            choice = await request_notes_offer(self.telegram, self.redis, self.chat_id, name)
            if choice in ("expired", "none"):
                return choice
            await say(self.telegram, self.chat_id, "⏳ Preparing the notes… this takes a minute or two.")
            await process_recording(
                wav, seconds, chat_id=self.chat_id, channel_name=name, bot=self.telegram, session_factory=self.sessions,
                redis=self.redis, task_queue=self.task_queue, choice=choice,
            )
            return choice
        except Exception:
            logger.exception("the notes for a recording failed")
            await say(self.telegram, self.chat_id, "❌ I couldn't prepare the notes for that recording.")
            return "failed"

    def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()

    # -- the loop ------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                item = await self.redis.blpop(EVENTS_KEY, timeout=2)
            except Exception:
                logger.exception("reading recorder events failed")
                await asyncio.sleep(2)
                continue
            if not item:
                continue
            try:
                outcome = await self.handle(json.loads(item[1]))
                logger.info("recorder event", extra={"outcome": outcome})
            except Exception:
                logger.exception("recorder event failed")
        await self.drain()
