"""The Discord controller (the discord-recorder service's counterpart): joins and leaves become
Telegram prompts and start/stop commands, nothing is recorded without a Yes, and a finished
recording goes to the chat. Redis is real; the recorder and Telegram are fakes."""

import asyncio
import json
import time
import wave

import pytest

from app.core.config import settings
from app.discord_controller import COMMANDS_KEY, Controller, recordings_dir
from app.services.discord_voice import Phase
from app.services.telegram_center import CommandCenter
from tests.support import create_workflow
from tests.test_discord_voice import TRANSCRIPT, _buttons, _documents, meeting_graph
from tests.test_telegram_center import CHAT, FakeBot, InlineQueue, tap, uid


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture(autouse=True)
async def _clean_keys(redis):
    yield
    for pattern in ("flowforge:telegram:*", "flowforge:discord:*"):
        async for key in redis.scan_iter(pattern):
            await redis.delete(key)


@pytest.fixture
async def controller(bot, redis, session_factory):
    controller = Controller(bot, redis, session_factory, CHAT)
    controller.task_queue = InlineQueue(session_factory, redis)  # runs the notes pipeline in-process
    yield controller
    controller.close()


@pytest.fixture
def center(bot, redis, session_factory):
    return CommandCenter(bot, session_factory=session_factory, redis=redis, task_queue=InlineQueue(session_factory, redis))


def event(kind, **fields):
    return {"type": kind, "ts": int(time.time() * 1000), "channel_name": "Study_Group", **fields}


async def commands(redis):
    return [json.loads(item) for item in await redis.lrange(COMMANDS_KEY, 0, -1)]


async def wait_for_prompt(bot, count=1):
    for _ in range(300):
        if len(bot.sent) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no Telegram prompt was sent")


async def joined(controller, bot, user_id=1):
    assert await controller.handle(event("member_joined", user_id=user_id)) == "asking"
    await wait_for_prompt(bot)


def wav_bytes(seconds=40, level=3000):
    import io

    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        samples = (b"".join((level if (i // 40) % 2 else -level).to_bytes(2, "little", signed=True) for i in range(160)) * (seconds * 100))
        w.writeframes(samples)
    return out.getvalue()


@pytest.fixture
def files_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILES_DIR", str(tmp_path))
    recordings_dir().mkdir(parents=True)
    return recordings_dir()


# --- Asking and starting ---------------------------------------------------------------------


async def test_the_first_person_in_an_empty_channel_triggers_the_prompt_and_nothing_starts_yet(controller, bot, redis):
    await joined(controller, bot)
    assert "Voice activity detected in #Study_Group — record and send you this meeting?" in bot.sent[0]["text"]
    assert await commands(redis) == []  # no recording without a Yes
    controller.tracker.left(1)  # let the pending prompt end
    await controller.drain()


async def test_yes_starts_the_recorder_with_the_session_and_limits(controller, bot, redis, center):
    await joined(controller, bot)
    assert await center.handle_update(tap(uid(), _buttons(bot)["✅ Yes, record"])) == "voice_yes"
    await controller.drain()
    (command,) = await commands(redis)
    assert command == {"type": "start_recording", "session": 1, "max_minutes": settings.DISCORD_RECORDING_MAX_MINUTES,
                       "output": settings.DISCORD_OUTPUT}
    assert controller.tracker.phase is Phase.RECORDING


async def test_no_records_nothing_and_the_same_session_is_not_asked_again(controller, bot, redis, center):
    await joined(controller, bot)
    assert await center.handle_update(tap(uid(), _buttons(bot)["✖ No"])) == "voice_no"
    await controller.drain()
    assert await commands(redis) == []
    for kind, user in (("member_joined", 2), ("member_left", 2), ("member_joined", 3), ("member_left", 1)):
        assert await controller.handle(event(kind, user_id=user)) == "none"
    assert len(bot.sent) == 1  # one prompt for the whole session
    # Everyone leaves: the session ends. The next join is a new session and asks again.
    await controller.handle(event("member_left", user_id=3))
    assert controller.tracker.phase is Phase.IDLE
    await controller.handle(event("member_joined", user_id=1))
    await wait_for_prompt(bot, 2)
    assert controller.tracker.session == 2
    controller.tracker.left(1)
    await controller.drain()


async def test_everyone_leaving_while_recording_sends_stop(controller, bot, redis, center):
    await joined(controller, bot)
    await center.handle_update(tap(uid(), _buttons(bot)["✅ Yes, record"]))
    await controller.drain()
    assert await controller.handle(event("member_left", user_id=1)) == "stopping"
    assert (await commands(redis))[-1] == {"type": "stop_recording", "reason": "the channel is empty"}


async def test_people_already_in_the_channel_at_startup_are_not_asked_about(controller, bot, redis):
    await controller.handle(event("snapshot", user_ids=[1, 2]))
    assert await controller.handle(event("member_joined", user_id=3)) == "none"
    assert not bot.sent and await commands(redis) == []


async def test_events_from_before_a_controller_restart_are_ignored(controller, bot):
    old = event("member_joined", user_id=1)
    old["ts"] -= 10 * 60 * 1000
    assert await controller.handle(old) == "stale"
    assert not bot.sent


async def test_a_recorder_that_cant_start_tells_the_chat_and_the_session_is_not_retried(controller, bot, redis, center):
    await joined(controller, bot)
    await center.handle_update(tap(uid(), _buttons(bot)["✅ Yes, record"]))
    await controller.drain()
    assert await controller.handle(event("recording_failed", session=1, reason="Missing Permissions")) == "failed"
    assert "couldn't start recording #Study_Group (Missing Permissions)" in bot.texts()[-1]
    assert controller.tracker.phase is Phase.DECLINED


# --- Delivery ----------------------------------------------------------------------------------


def ready(path, **fields):
    return event("recording_ready", session=1, path=str(path), seconds=40.0, speech_seconds=12.0, frames=600,
                 has_audio=True, reason="the channel is empty", **fields)


async def test_the_finished_recording_goes_to_the_chat_as_an_mp3_and_the_file_is_removed(controller, bot, files_dir):
    path = files_dir / "rec.wav"
    path.write_bytes(wav_bytes())
    assert await controller.handle(ready(path)) == "sent"
    (doc,) = _documents(bot)
    assert doc["chat_id"] == CHAT and doc["document"].endswith(".mp3") and doc["content_type"] == "audio/mpeg"
    assert "Recording of #Study_Group (0.7 min)" in doc["text"]
    assert not path.exists()


async def test_a_recording_with_no_audio_is_reported_not_sent(controller, bot, files_dir):
    path = files_dir / "silent.wav"
    path.write_bytes(wav_bytes())
    event_ = ready(path)
    event_["has_audio"] = False
    assert await controller.handle(event_) == "no_audio"
    assert "heard only 12 seconds of speech" in bot.texts()[-1] and not _documents(bot)
    assert not path.exists()


async def test_a_path_outside_the_recordings_directory_is_never_read(controller, bot, files_dir, tmp_path):
    outside = tmp_path / "secret.wav"
    outside.write_bytes(wav_bytes())
    assert await controller.handle(ready(outside)) == "missing"
    assert not _documents(bot) and outside.exists()
    assert await controller.handle(ready(files_dir / "../secret.wav")) == "missing"


async def test_a_recording_cut_off_by_the_time_cap_is_not_asked_about_again(controller, bot, files_dir, center):
    await joined(controller, bot)
    await center.handle_update(tap(uid(), _buttons(bot)["✅ Yes, record"]))
    await controller.drain()
    path = files_dir / "long.wav"
    path.write_bytes(wav_bytes())
    event_ = ready(path)
    event_["reason"] = "max_duration"
    await controller.handle(event_)
    assert controller.tracker.phase is Phase.DONE
    assert await controller.handle(event("member_joined", user_id=2)) == "none"


# --- Notes offered after the audio -----------------------------------------------------------


async def delivered(controller, bot, files_dir):
    path = files_dir / "rec.wav"
    path.write_bytes(wav_bytes())
    assert await controller.handle(ready(path)) == "sent"
    await wait_for_prompt(bot, 2)  # the audio, then the offer
    return {b["text"]: b["callback_data"] for b in bot.sent[-1]["reply_markup"]["inline_keyboard"][0]}


async def test_after_the_audio_the_chat_is_offered_summary_transcript_both_or_no_thanks(controller, bot, files_dir):
    buttons = await delivered(controller, bot, files_dir)
    assert set(buttons) == {"📝 Summary", "📄 Full transcript", "Both", "✖ No thanks"}
    assert "Want notes for this recording of #Study_Group?" in bot.sent[-1]["text"]
    assert len(_documents(bot)) == 1  # the audio came first, and nothing was summarized yet


async def test_summary_runs_the_pipeline_on_the_same_recording_and_sends_only_the_summary(
        client, user, controller, bot, files_dir, center):
    await create_workflow(client, user, meeting_graph(), name="Meeting Notes")
    buttons = await delivered(controller, bot, files_dir)
    assert await center.handle_update(tap(uid(), buttons["📝 Summary"])) == "voice_summary"
    await controller.drain()
    assert any("Preparing the notes" in t for t in bot.texts())
    assert "We shipped it." in bot.texts()[-1] and len(_documents(bot)) == 1  # still just the audio file


async def test_transcript_and_both_send_the_transcript_as_a_text_file(client, user, controller, bot, files_dir, center):
    await create_workflow(client, user, meeting_graph(), name="Meeting Notes")
    buttons = await delivered(controller, bot, files_dir)
    assert await center.handle_update(tap(uid(), buttons["Both"])) == "voice_both"
    await controller.drain()
    docs = _documents(bot)
    assert len(docs) == 2 and docs[1]["document"].endswith(".txt") and docs[1]["data"].decode() == TRANSCRIPT
    assert any("We shipped it." in (t or "") for t in bot.texts())


async def test_no_thanks_runs_nothing(client, user, controller, bot, files_dir, center, shared_session):
    from sqlalchemy import select

    from app.models.execution import WorkflowExecution

    await create_workflow(client, user, meeting_graph(), name="Meeting Notes")
    buttons = await delivered(controller, bot, files_dir)
    assert await center.handle_update(tap(uid(), buttons["✖ No thanks"])) == "voice_none"
    await controller.drain()
    assert (await shared_session.scalars(select(WorkflowExecution))).first() is None
    assert "No notes" in bot.edits[-1][2]
