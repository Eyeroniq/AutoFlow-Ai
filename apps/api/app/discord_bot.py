"""The discord-bot service: watches one voice channel and, with a yes from Telegram, records it.

    python -m app.discord_bot

Connects to the Discord gateway with DISCORD_BOT_TOKEN and watches only DISCORD_MONITOR_CHANNEL_ID
in DISCORD_MONITOR_GUILD_ID. When the channel goes from empty to non-empty it asks the Telegram
chat (TELEGRAM_CHAT_ID) whether to record; only a Yes tap (handled by the telegram-listener, see
app.services.telegram_center) makes it join, post the consent notice in the channel's text chat,
and record. The recording ends when the channel empties or after DISCORD_RECORDING_MAX_MINUTES,
then the recording goes to the same Telegram chat as an audio file (DISCORD_OUTPUT=audio, the default)
or is summarized by the Meeting Notes pipeline (app.services.discord_voice.process_recording, DISCORD_OUTPUT=notes).
The decision logic lives in app.services.discord_voice; this file is the py-cord glue.

py-cord 2.8's voice receive is unfinished: its Sink lacks is_opus(), and its RTP/DAVE/Opus handling
loses the start of every frame.
`VoiceSink` supplies what's missing and `_patch_receive` fixes the rest (see its docstring).
"""

import asyncio
import contextlib
import logging
import signal
import time
from typing import Any

import discord
from discord.sinks import Sink
from flowforge_engine.providers.telegram_provider import TelegramProvider
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.logging import register_secret, setup_logging
from app.core.redis import new_redis
from app.discord_controller import Controller
from app.services.discord_voice import (
    Action,
    Phase,
    MixedRecorder,
    SessionTracker,
    process_recording,
    send_recording,
    request_consent,
    say,
)
from app.services.task_queue import CeleryTaskQueue

logger = logging.getLogger("app.discord_bot")

CONSENT_NOTICE = "🔴 Recording started for meeting notes, per request"
# Less speech than this in a recording and nothing is summarized.
MIN_SPEECH_SECONDS = 3
STOPPED_NOTICE = "⏹️ Recording ended. The audio recording will be sent to the person who approved it."
STOPPED_NOTICE_NOTES = "⏹️ Recording ended. A summary will be sent to the person who approved it."


class VoiceSink(Sink):
    """Feeds every speaker's decoded PCM into a MixedRecorder."""

    # What py-cord's event router expects of a sink: no event listeners, no child sinks.
    __sink_listeners__: list[tuple[str, str]] = []

    def __init__(self, recorder: MixedRecorder, voice: Any):
        super().__init__()
        self.recorder = recorder
        # py-cord 2.8's reader no longer attaches the voice client to the sink; its decoder needs it.
        self.vc = voice

    def walk_children(self, *, with_self: bool = False) -> list["VoiceSink"]:
        return [self] if with_self else []

    def is_opus(self) -> bool:
        return False  # we want decoded PCM

    def write(self, data: Any, user: Any) -> None:  # noqa: D102 (py-cord's Sink.write; runs on its receive thread)
        pcm = getattr(data, "pcm", None)
        if pcm:
            packet = getattr(data, "packet", None)
            self.recorder.add(int(getattr(user, "id", 0) or 0), pcm, getattr(packet, "timestamp", None))

    def cleanup(self) -> None:
        self.finished = True


# What the receive patches saw, logged when a recording stops (it shows where audio is lost).
RECEIVE_STATS: dict[str, int] = {"decoded": 0, "undecodable": 0, "unknown_speaker": 0, "dave_failed": 0}
# Decrypted (ok) and failed frames per 5 seconds of recording, to see when audio is lost.
RECEIVE_TIMELINE: dict[Any, Any] = {"t0": 0.0}


def _patch_receive() -> None:
    """py-cord 2.8's receive path has three faults, patched here:

    1. With the `aead_xchacha20_poly1305_rtpsize` mode Discord uses, it cuts a hard-coded 8 bytes
       (the RTP header extension) off every frame, and with DAVE end-to-end encryption on it cuts
       the extension off a second time. Frames lose their first bytes and Opus reports
       "corrupted stream". The extension's real length is in the packet header.
    2. Its decoder runs DAVE decryption again on the decoded PCM.
    3. One undecodable frame kills the receive thread.
    """
    import struct

    import davey
    from discord import opus
    from discord.voice.packets.core import OPUS_SILENCE
    from discord.voice.receive.reader import CryptoError, PacketDecryptor

    def decrypt_aead(self: Any, packet: Any) -> bytes:
        packet.adjust_rtpsize()
        try:
            result = self.box.decrypt(bytes(packet.data), bytes(packet.header), packet.nonce + bytes(20))
        except Exception as exc:
            raise CryptoError(exc) from exc
        packet._plain = result  # before the extension is cut off, for the retry in decrypt_rtp
        if packet.extended:
            words = struct.unpack(">H", bytes(packet.header[-2:]))[0]
            packet._ext_bytes = words * 4
            return result[words * 4:]
        packet._ext_bytes = 0
        return result

    probe_decoder = opus.Decoder()

    def probe(packet: Any) -> None:
        """Diagnostics: which cut of the decrypted payload is valid Opus? (counts in RECEIVE_STATS)"""
        plain = getattr(packet, "_plain", None)
        if plain is None:
            return
        RECEIVE_STATS["probe_frames"] = RECEIVE_STATS.get("probe_frames", 0) + 1
        ext = getattr(packet, "_ext_bytes", 0)
        RECEIVE_STATS[f"ext_{ext}"] = RECEIVE_STATS.get(f"ext_{ext}", 0) + 1
        for offset in (0, 4, 8, 12, 16):
            try:
                probe_decoder.decode(plain[offset:], fec=False)
            except Exception:
                continue
            RECEIVE_STATS[f"opus_ok_cut_{offset}"] = RECEIVE_STATS.get(f"opus_ok_cut_{offset}", 0) + 1

    def timeline(ok: bool) -> None:
        bucket = int(time.monotonic() - RECEIVE_TIMELINE["t0"]) // 5 * 5
        RECEIVE_TIMELINE.setdefault(bucket, [0, 0])[0 if ok else 1] += 1

    def decrypt_rtp(self: Any, packet: Any) -> bytes | None:
        state = self.client._connection
        data = self._decryptor_rtp(packet)
        dave = state.dave_session
        if data != OPUS_SILENCE:
            probe(packet)
        if dave is not None and dave.ready and data != OPUS_SILENCE:
            user_id = state.ssrc_user_map.get(packet.ssrc)
            if not user_id:
                RECEIVE_STATS["unknown_speaker"] += 1
                return None
            try:
                data = dave.decrypt(user_id, davey.MediaType.audio, data)
                timeline(True)
            except Exception as exc:
                plain = getattr(packet, "_plain", None)
                kind = "unencrypted" if "UnencryptedWhenPassthroughDisabled" in str(exc) else "no_valid_cryptor" if "NoValidCryptorFound" in str(exc) else "other"
                RECEIVE_STATS[f"err_{kind}"] = RECEIVE_STATS.get(f"err_{kind}", 0) + 1
                if kind == "unencrypted":
                    # Plain Opus, not DAVE-wrapped (the sender is still in passthrough, e.g. just after
                    # the bot joined or on an epoch change): it is audio as it is.
                    timeline(True)
                    packet.decrypted_data = data
                    return data
                recovered = None
                if plain is not None:
                    # Is the extension cut at the wrong place? Try other offsets.
                    for offset in (0, 4, 8, 12, 16):
                        if offset == packet._ext_bytes:
                            continue
                        try:
                            recovered = dave.decrypt(user_id, davey.MediaType.audio, plain[offset:])
                            RECEIVE_STATS[f"recovered_cut_{offset}_was_{packet._ext_bytes}"] = (
                                RECEIVE_STATS.get(f"recovered_cut_{offset}_was_{packet._ext_bytes}", 0) + 1)
                            break
                        except Exception:
                            continue
                if recovered is not None:
                    data = recovered
                    timeline(True)
                else:
                    RECEIVE_STATS["dave_failed"] += 1
                    timeline(False)
                    if RECEIVE_STATS["dave_failed"] <= 3:
                        logger.warning(
                            "DAVE decrypt failed: %r (ssrc %s, extended %s, ext bytes %s, plain %s bytes, tail %s, epoch %s)",
                            exc, packet.ssrc, packet.extended, getattr(packet, "_ext_bytes", None), len(plain or b""),
                            (plain or b"")[-4:].hex(), getattr(dave, "epoch", None))
                    return None
        packet.decrypted_data = data
        return data

    def _decode_packet(self: Any, packet: Any) -> Any:
        try:
            if packet:
                pcm = self._decoder.decode(packet.decrypted_data, fec=False)
            else:
                following = self._buffer.peek_next()
                pcm = self._decoder.decode(following.decrypted_data if following is not None else None,
                                           fec=following is not None)
            RECEIVE_STATS["decoded"] += 1
        except Exception:
            RECEIVE_STATS["undecodable"] += 1
            logger.debug("skipping an undecodable voice frame", exc_info=True)
            pcm = b""
        return packet, pcm

    PacketDecryptor._decrypt_rtp_aead_xchacha20_poly1305_rtpsize = decrypt_aead
    PacketDecryptor.decrypt_rtp = decrypt_rtp
    opus.PacketDecoder._decode_packet = _decode_packet


class Monitor:
    def __init__(self, bot: discord.Bot, telegram: TelegramProvider, chat_id: str, redis: Any, sessions: Any):
        self.bot, self.telegram, self.chat_id, self.redis, self.sessions = bot, telegram, chat_id, redis, sessions
        self.guild_id = settings.DISCORD_MONITOR_GUILD_ID
        self.channel_id = settings.DISCORD_MONITOR_CHANNEL_ID
        self.tracker = SessionTracker()
        self.recorder: MixedRecorder | None = None
        self.voice: discord.VoiceClient | None = None
        self.record_session = 0
        self.tasks: set[asyncio.Task[Any]] = set()
        self._ready = False
        self._cap: asyncio.Task[Any] | None = None

    # -- gateway events ------------------------------------------------------------------------

    def channel(self) -> Any:
        return self.bot.get_channel(self.channel_id)

    async def on_ready(self) -> None:
        if self._ready:
            return
        channel = self.channel()
        if channel is None or channel.guild.id != self.guild_id:
            logger.error("the monitored channel isn't visible to the bot; check the guild/channel ids and the invite")
            return
        present = {m.id for m in channel.members if not m.bot}
        self.tracker = SessionTracker(present)
        self._ready = True
        logger.info("watching voice channel", extra={"channel": channel.name, "already_present": len(present)})

    async def on_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        if not self._ready or member.bot or member.guild.id != self.guild_id:
            return
        was = before.channel is not None and before.channel.id == self.channel_id
        now = after.channel is not None and after.channel.id == self.channel_id
        if was == now:
            return
        action = self.tracker.joined(member.id) if now else self.tracker.left(member.id)
        if action is Action.ASK:
            self._spawn(self._ask(self.tracker.session))
        elif action is Action.STOP:
            self._spawn(self._stop_recording("the channel is empty"))

    def _spawn(self, work: Any) -> None:
        task = asyncio.ensure_future(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # -- asking, recording ---------------------------------------------------------------------

    async def _ask(self, session: int) -> None:
        channel = self.channel()
        try:
            answer = await request_consent(
                self.telegram, self.redis, self.chat_id, channel.name, is_current=lambda: self.tracker.session == session and self.tracker.phase is Phase.ASKING,
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
        await self._start_recording(session)

    async def _start_recording(self, session: int) -> None:
        channel = self.channel()
        try:
            self.voice = await channel.connect(timeout=30)
            # The visible notice comes first: no notice, no recording.
            await channel.send(CONSENT_NOTICE)
        except Exception as exc:
            logger.exception("couldn't join or post the notice; not recording")
            await self._leave()
            self.tracker.declined(session)
            await say(self.telegram, self.chat_id,
                      f"I couldn't start recording #{channel.name} ({type(exc).__name__}: {exc}). "
                      "Check that the bot has Connect, Speak, and Send Messages there. Nothing was recorded.")
            return
        self.recorder = MixedRecorder()
        self.recorder.start()
        for key in list(RECEIVE_STATS):
            if key.startswith("recovered"):
                del RECEIVE_STATS[key]
            else:
                RECEIVE_STATS[key] = 0
        RECEIVE_TIMELINE.clear()
        RECEIVE_TIMELINE["t0"] = time.monotonic()
        self.record_session = session
        done = asyncio.get_running_loop()
        try:
            self.voice.start_recording(
                VoiceSink(self.recorder, self.voice), lambda error: done.call_soon_threadsafe(self._on_reader_stopped, error)
            )
        except Exception as exc:
            logger.exception("start_recording failed")
            self.recorder = None
            with contextlib.suppress(Exception):
                await channel.send("⚠️ Recording could not start; nothing is being recorded.")
            await self._leave()
            self.tracker.declined(session)
            await say(self.telegram, self.chat_id,
                      f"I joined #{channel.name} but recording failed to start ({type(exc).__name__}: {exc}). Nothing was recorded.")
            return
        self._cap = asyncio.ensure_future(self._max_duration(session))
        logger.info("recording started", extra={"channel": channel.name})

    def _on_reader_stopped(self, error: Exception | None) -> None:
        if error:
            logger.error("the voice reader stopped with an error", extra={"error": str(error)})

    async def _max_duration(self, session: int) -> None:
        await asyncio.sleep(settings.DISCORD_RECORDING_MAX_MINUTES * 60)
        self.tracker.finished(session)
        await self._stop_recording("the maximum duration was reached")

    async def _stop_recording(self, reason: str) -> None:
        recorder, self.recorder = self.recorder, None
        if recorder is None:
            return
        if self._cap is not None and self._cap is not asyncio.current_task():
            self._cap.cancel()
        self._cap = None
        recorder.stop()
        channel = self.channel()
        logger.info("recording stopped", extra={"reason": reason, "seconds": round(recorder.seconds), "frames": recorder.frames, "speech_seconds": round(recorder.frames * 0.02, 1), **RECEIVE_STATS,
                    "timeline": {k: v for k, v in RECEIVE_TIMELINE.items() if k != "t0"}})
        with contextlib.suppress(Exception):
            if self.voice is not None and self.voice.is_recording():
                self.voice.stop_recording()
        with contextlib.suppress(Exception):
            await channel.send(STOPPED_NOTICE_NOTES if settings.DISCORD_OUTPUT == "notes" else STOPPED_NOTICE)
        await self._leave()
        heard = recorder.frames * 0.02  # seconds of speech received (per speaker)
        notes = settings.DISCORD_OUTPUT == "notes"
        if recorder.seconds >= settings.DISCORD_MIN_RECORDING_SECONDS and (
            not recorder.has_audio() or (notes and heard < MIN_SPEECH_SECONDS)
        ):
            await say(self.telegram, self.chat_id,
                      f"I recorded #{channel.name} for {recorder.seconds / 60:.1f} minutes but heard only {heard:.0f} seconds "
                      "of speech, so there is nothing worth summarizing (Whisper invents text from silence).")
            return
        if not notes:
            await send_recording(
                recorder.to_wav(), recorder.seconds, chat_id=self.chat_id, channel_name=channel.name,
                bot=self.telegram, session_factory=self.sessions,
            )
            return
        await process_recording(
            recorder.to_wav(), recorder.seconds, chat_id=self.chat_id, channel_name=channel.name, bot=self.telegram,
            session_factory=self.sessions, redis=self.redis, task_queue=CeleryTaskQueue(),
        )

    async def _leave(self) -> None:
        voice, self.voice = self.voice, None
        if voice is not None:
            with contextlib.suppress(Exception):
                await voice.disconnect(force=True)


async def run(stop: asyncio.Event) -> None:
    token = settings.DISCORD_BOT_TOKEN.get_secret_value().strip() if settings.DISCORD_BOT_TOKEN else ""
    telegram_token = settings.TELEGRAM_BOT_TOKEN.get_secret_value().strip() if settings.TELEGRAM_BOT_TOKEN else ""
    chat_id = str(settings.TELEGRAM_CHAT_ID or "").strip()
    missing = [name for name, value in (
        ("DISCORD_BOT_TOKEN", token), ("TELEGRAM_BOT_TOKEN", telegram_token), ("TELEGRAM_CHAT_ID", chat_id),
        # The recorder's channel comes from a Discord Voice Meeting block; only the py-cord monitor needs ids in .env.
        *((name, value) for name, value in (
            ("DISCORD_MONITOR_GUILD_ID", settings.DISCORD_MONITOR_GUILD_ID),
            ("DISCORD_MONITOR_CHANNEL_ID", settings.DISCORD_MONITOR_CHANNEL_ID),
        ) if settings.DISCORD_RECORDER != "node"),
    ) if not value]
    if missing:
        logger.warning("the Discord voice monitor is off; set %s", ", ".join(missing))
        await stop.wait()
        return
    if settings.PUBLIC_DEMO and not settings.DEMO_OWNER_EMAIL.strip():
        # The recorder is the owner's: in a public demo it runs only for DEMO_OWNER_EMAIL's account.
        logger.error("PUBLIC_DEMO is on but DEMO_OWNER_EMAIL isn't set; the Discord voice monitor refuses to run")
        await stop.wait()
        return
    if settings.DISCORD_RECORDER == "node":
        # The discord-recorder service (discord.js) is in the voice channel; this process only decides.
        redis = new_redis()
        engine = create_async_engine(settings.DATABASE_URL, pool_size=5)
        controller = Controller(
            TelegramProvider(telegram_token), redis, async_sessionmaker(engine, expire_on_commit=False, autoflush=False), chat_id,
        )
        logger.info("Discord voice controller started; recording is done by the discord-recorder service")
        try:
            await controller.run(stop)
        finally:
            await redis.aclose()
            await engine.dispose()
        return
    _patch_receive()
    intents = discord.Intents.none()
    intents.guilds = True
    intents.voice_states = True
    bot = discord.Bot(intents=intents)
    redis = new_redis()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=5)
    monitor = Monitor(
        bot, TelegramProvider(telegram_token), chat_id, redis,
        async_sessionmaker(engine, expire_on_commit=False, autoflush=False),
    )
    bot.add_listener(monitor.on_ready, "on_ready")
    bot.add_listener(monitor.on_voice_state_update, "on_voice_state_update")
    runner = asyncio.ensure_future(bot.start(token))
    try:
        await asyncio.wait({runner, asyncio.ensure_future(stop.wait())}, return_when=asyncio.FIRST_COMPLETED)
        if runner.done() and not stop.is_set():
            runner.result()  # a login failure, say: raise it so the container restarts with the reason in its log
    finally:
        if monitor.recorder is not None:
            await monitor._stop_recording("the service is shutting down")
        for task in list(monitor.tasks):
            task.cancel()
        await bot.close()
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    setup_logging(settings.LOG_LEVEL)
    for secret in settings.secret_values():
        register_secret(secret)
    stop = asyncio.Event()

    async def go() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await run(stop)

    asyncio.run(go())


if __name__ == "__main__":
    main()
