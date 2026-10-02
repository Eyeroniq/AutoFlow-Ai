"""Discord voice monitoring: empty-to-non-empty session detection, the once-per-session prompt,
signed Yes/No confirmations (expiry, chat mismatch, forgery), the mixed recording, and the
handoff to the Meeting Notes pipeline. Discord itself isn't involved: the bot is a fake."""

import asyncio
import io
import time
import wave

import numpy as np
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.enums import ExecutionTrigger
from app.models.execution import WorkflowExecution
from app.services.discord_voice import (
    Action,
    find_transcript,
    MixedRecorder,
    Phase,
    SessionTracker,
    process_recording,
    request_consent,
    request_output_choice,
    send_recording,
    mute_telegram,
    summary_text,
)
from app.services.telegram_center import (
    PENDING_KEY,
    VOICE_DECISION_KEY,
    CommandCenter,
    ConfirmationError,
    callback_data,
    verify_callback,
)
from app.services.files import file_path
from app.models.file import UploadedFile
from tests.support import create_workflow
from tests.test_telegram_center import CHAT, OTHER_CHAT, FakeBot, InlineQueue, tap, uid


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture(autouse=True)
async def _clean_keys(redis):
    yield
    for pattern in ("flowforge:telegram:*", "flowforge:discord:*"):
        async for key in redis.scan_iter(pattern):
            await redis.delete(key)


# --- Session tracking -------------------------------------------------------------------------


def test_a_session_starts_when_the_channel_goes_from_empty_to_non_empty():
    tracker = SessionTracker()
    assert tracker.joined(1) is Action.ASK
    assert tracker.phase is Phase.ASKING and tracker.session == 1
    assert tracker.joined(2) is Action.NONE  # a second person joining isn't a new session
    assert tracker.left(1) is Action.NONE
    assert tracker.left(2) is Action.STOP  # empty again while a prompt was out
    assert tracker.phase is Phase.IDLE


def test_a_declined_or_expired_prompt_is_not_repeated_during_the_same_session():
    tracker = SessionTracker()
    assert tracker.joined(1) is Action.ASK
    tracker.declined(tracker.session)
    # People come and go for an hour but the channel never empties: no new prompt.
    assert [tracker.joined(2), tracker.left(2), tracker.joined(3), tracker.left(1), tracker.joined(1)] == [Action.NONE] * 5
    assert tracker.left(1) is Action.NONE and tracker.left(3) is Action.NONE  # declined sessions end quietly
    # A later, separate session asks again.
    assert tracker.joined(1) is Action.ASK and tracker.session == 2


def test_approval_only_counts_for_the_session_that_was_asked():
    tracker = SessionTracker()
    tracker.joined(1)
    asked = tracker.session
    tracker.left(1)  # everyone left while the prompt was out
    tracker.joined(1)  # a new session begins
    assert tracker.approved(asked) is False  # the stale "yes" is ignored
    assert tracker.approved(tracker.session) is True and tracker.phase is Phase.RECORDING
    assert tracker.approved(tracker.session) is False  # and only once


def test_recording_ends_when_the_channel_empties_and_a_capped_one_isnt_re_asked():
    tracker = SessionTracker()
    tracker.joined(1)
    tracker.approved(tracker.session)
    assert tracker.left(1) is Action.STOP
    tracker.joined(1)
    tracker.approved(tracker.session)
    tracker.finished(tracker.session)  # the maximum duration was reached
    assert tracker.phase is Phase.DONE
    assert tracker.joined(2) is Action.NONE and tracker.left(1) is Action.NONE
    assert tracker.left(2) is Action.NONE and tracker.joined(1) is Action.ASK


def test_people_already_in_the_channel_at_startup_are_not_asked_about():
    tracker = SessionTracker({1, 2})
    assert tracker.phase is Phase.DECLINED
    assert tracker.joined(3) is Action.NONE
    tracker.left(1), tracker.left(2)
    assert tracker.left(3) is Action.NONE
    assert tracker.joined(1) is Action.ASK


# --- Confirmation (the Command Center's signed tokens) -----------------------------------------


def _buttons(bot):
    row = bot.sent[-1]["reply_markup"]["inline_keyboard"][0]
    return {b["text"]: b["callback_data"] for b in row}


async def _prompt(bot, redis, **kwargs):
    import asyncio

    task = asyncio.ensure_future(request_consent(bot, redis, CHAT, "meeting-room", poll_seconds=0.01, **kwargs))
    for _ in range(200):
        if bot.sent:
            break
        await asyncio.sleep(0.01)
    return task


async def test_the_prompt_names_the_channel_and_a_yes_tap_approves(bot, redis, session_factory):
    task = await _prompt(bot, redis)
    assert bot.sent[0]["chat_id"] == CHAT
    assert "Voice activity detected in #meeting-room — record and send you this meeting?" in bot.sent[0]["text"]
    yes = _buttons(bot)["✅ Yes, record"]
    assert len(yes.encode()) <= 64
    center = CommandCenter(bot, session_factory=session_factory, redis=redis, task_queue=InlineQueue(session_factory, redis))
    assert await center.handle_update(tap(uid(), yes)) == "voice_yes"
    assert await task == "yes"
    assert bot.edits[-1][2].startswith("✅ Approved")
    # The pending request was single-use: a second tap does nothing.
    assert await center.handle_update(tap(uid(), yes)) == "already_handled"


async def test_a_no_tap_declines(bot, redis, session_factory):
    task = await _prompt(bot, redis)
    center = CommandCenter(bot, session_factory=session_factory, redis=redis, task_queue=InlineQueue(session_factory, redis))
    assert await center.handle_update(tap(uid(), _buttons(bot)["✖ No"])) == "voice_no"
    assert await task == "no"
    assert "nothing will be recorded" in bot.edits[-1][2]


async def test_an_unanswered_prompt_expires_and_a_late_tap_is_refused(bot, redis, session_factory):
    clock = {"now": 1_000_000.0}

    async def sleep(seconds):
        clock["now"] += 60  # a minute passes per poll

    task = await _prompt(bot, redis, now=lambda: clock["now"], sleep=sleep)
    assert await task == "expired"
    assert "No answer in time" in bot.edits[-1][2]
    yes = _buttons(bot)["✅ Yes, record"]
    center = CommandCenter(bot, session_factory=session_factory, redis=redis, task_queue=InlineQueue(session_factory, redis))
    assert await center.handle_update(tap(uid(), yes)) == "expired"  # the signed expiry (set from the fake clock) has passed
    assert await redis.get(PENDING_KEY.format(yes.split(":")[1])) is None
    with pytest.raises(ConfirmationError, match="expired"):
        verify_callback(yes, CHAT, now=clock["now"] + settings.TELEGRAM_CONFIRM_SECONDS + 1)


async def test_a_tap_from_another_chat_or_a_forged_token_is_rejected(bot, redis, session_factory):
    task = await _prompt(bot, redis)
    yes = _buttons(bot)["✅ Yes, record"]
    center = CommandCenter(bot, session_factory=session_factory, redis=redis, task_queue=InlineQueue(session_factory, redis))
    assert await center.handle_update(tap(uid(), yes, chat=OTHER_CHAT)) == "invalid"
    assert await center.handle_update(tap(uid(), yes[:-3] + "AAA")) == "invalid"
    # "n" instead of "v" with the same signature is a different, unsigned action.
    assert await center.handle_update(tap(uid(), "n" + yes[1:])) == "invalid"
    assert await redis.get(VOICE_DECISION_KEY.format(yes.split(":")[1])) is None
    task.cancel()


async def test_a_voice_button_cannot_confirm_a_pipeline_run_and_vice_versa(bot, redis, session_factory):
    center = CommandCenter(bot, session_factory=session_factory, redis=redis, task_queue=InlineQueue(session_factory, redis))
    # A confirmation record for a pipeline run, answered with a (correctly signed) voice action.
    from app.core.crypto import get_cipher
    expires = int(time.time()) + 300
    await redis.set(PENDING_KEY.format("run123"), get_cipher().encrypt(
        {"chat_id": CHAT, "deployment_id": "x", "inputs": {}, "expires": expires}))
    assert await center.handle_update(tap(uid(), callback_data("v", "run123", CHAT, expires))) == "invalid"


def test_voice_tokens_are_chat_bound():
    expires = int(time.time()) + 300
    data = callback_data("v", "abc123def456", CHAT, expires)
    assert verify_callback(data, CHAT) == ("v", "abc123def456")
    with pytest.raises(ConfirmationError):
        verify_callback(data, OTHER_CHAT)


# --- Recording ---------------------------------------------------------------------------------


def _tone(seconds: float, amplitude: int = 4000) -> bytes:
    """48 kHz stereo 16-bit PCM: a 440 Hz tone."""
    t = np.arange(int(48000 * seconds)) / 48000
    mono = (np.sin(2 * np.pi * 440 * t) * amplitude).astype("<i2")
    return np.repeat(mono, 2).tobytes()


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _read(wav: bytes) -> tuple[int, int, np.ndarray]:
    with wave.open(io.BytesIO(wav)) as w:
        return w.getframerate(), w.getnchannels(), np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")


def test_speakers_are_mixed_into_one_mono_16k_wav_with_gaps_kept():
    clock = Clock()
    rec = MixedRecorder(clock)
    rec.start()
    # Alice speaks for 1 s (50 frames), then is silent for 2 s; Bob speaks during Alice's second.
    frame_a, frame_b = _tone(0.02, 3000), _tone(0.02, 2000)
    for _ in range(50):
        clock.now += 0.02
        rec.add(1, frame_a)
        rec.add(2, frame_b)
    clock.now += 2.0
    for _ in range(25):  # Alice again after the gap
        clock.now += 0.02
        rec.add(1, frame_a)
    rec.stop()
    rate, channels, samples = _read(rec.to_wav())
    assert (rate, channels) == (16000, 1)
    assert len(samples) == int(rec.seconds * 16000)
    one_second = samples[: 16000]
    assert np.abs(one_second).max() > 4000  # both voices summed (3000 + 2000 peak, not clipped)
    gap = samples[int(1.2 * 16000): int(2.8 * 16000)]
    assert np.abs(gap).max() == 0  # silence stayed silent
    assert np.abs(samples[int(3.1 * 16000):]).max() > 2000  # Alice's later speech is placed after the gap
    assert rec.has_audio()


def test_an_empty_recording_has_no_audio_and_loud_overlap_is_clipped():
    rec = MixedRecorder(Clock())
    assert not rec.has_audio()
    clock = Clock()
    rec = MixedRecorder(clock)
    rec.start()
    clock.now += 0.02
    loud = _tone(0.02, 30000)
    rec.add(1, loud)
    rec.add(2, loud)
    assert np.abs(_read(rec.to_wav())[2]).max() <= 32767


# --- Handoff -----------------------------------------------------------------------------------



TRANSCRIPT = "Asha: let's ship on Friday. Ben: agreed, I will deploy."


def meeting_graph(sends=True):
    nodes = [{"id": "input", "type": "input", "config": {"name": "recording", "input_type": "file"}}]
    edges = []
    last = "input"
    if sends:
        nodes.append({"id": "send", "type": "telegram", "config": {"text": "Notes for {{input.value.filename}}"}})
        edges.append({"source": "input", "target": "send"})
        last = "send"
    nodes.append({"id": "out", "type": "output", "config": {"name": "meeting", "value": {
        "notes": {"summary": "We shipped it.", "decisions": ["Ship Friday"],
                  "action_items": [{"task": "Deploy", "owner": "Asha", "due": "Friday"}]},
        "transcript": TRANSCRIPT}}})
    edges.append({"source": last, "target": "out"})
    return {"nodes": nodes, "edges": edges}


def test_telegram_nodes_are_muted_so_the_bot_delivers_what_was_asked_for():
    graph = mute_telegram(meeting_graph())
    assert graph["nodes"][1]["config"]["auth"] == "mock"
    assert "auth" not in meeting_graph()["nodes"][1]["config"]  # the original is untouched


def test_summary_text_formats_meeting_notes_and_the_transcript_is_found():
    output = {"meeting": {"notes": {"summary": "S", "decisions": ["D"], "action_items": [
        {"task": "T", "owner": "O", "due": "Fri"}]}, "transcript": "hello there"}}
    text = summary_text(output)
    assert "S" in text and "- D" in text and "- T (owner: O, due: Fri)" in text
    assert find_transcript(output) == "hello there" and find_transcript({"x": 1}) is None


async def _handoff(client, user, bot, session_factory, redis, seconds, *, tap_label=None, graph=None,
                   name="Meeting Notes", session=None):
    """Runs the handoff; taps `tap_label` on the "what do you want" prompt when it appears."""
    await create_workflow(client, user, graph or meeting_graph(), name=name)
    center = CommandCenter(bot, session_factory=session_factory, redis=redis, task_queue=InlineQueue(session_factory, redis))
    task = asyncio.ensure_future(process_recording(
        _wav(seconds), seconds, chat_id=CHAT, channel_name="meeting-room", bot=bot, session_factory=session_factory,
        redis=redis, task_queue=InlineQueue(session_factory, redis), wait_seconds=10, choice_poll_seconds=0.01,
    ))
    if tap_label:
        for _ in range(500):
            prompt = next((m for m in bot.sent if "What do you want" in (m["text"] or "")), None)
            if prompt:
                break
            await asyncio.sleep(0.02)
        buttons = {b["text"]: b["callback_data"] for b in prompt["reply_markup"]["inline_keyboard"][0]}
        await center.handle_update(tap(uid(), buttons[tap_label]))
    return await task


def _wav(seconds=2.0):
    rec = MixedRecorder(Clock())
    rec.start()
    rec._clock.now += seconds  # type: ignore[attr-defined]
    rec.stop()
    return rec.to_wav()


def _documents(bot):
    return [m for m in bot.sent if m.get("document")]


def _notes(bot):
    return [m for m in bot.sent if m["text"] and "We shipped it." in m["text"]]


async def test_the_recording_runs_the_meeting_notes_pipeline_with_trigger_discord_voice(client, user, bot, session_factory, redis, shared_session):
    assert await _handoff(client, user, bot, session_factory, redis, 40, tap_label="📝 Summary") == "success"
    execution = (await shared_session.scalars(select(WorkflowExecution).order_by(WorkflowExecution.created_at.desc()))).first()
    assert execution.trigger is ExecutionTrigger.DISCORD_VOICE
    # The pipeline's own Telegram node was muted: only the bot sends, in the form that was asked for.
    send = next(n for n in execution.graph_json["nodes"] if n["id"] == "send")
    assert send["config"]["auth"] == "mock"
    # The recording is stored as the owner's upload, a real WAV, and fed to the file input.
    record = await shared_session.get(UploadedFile, execution.inputs_json["recording"])
    assert record.content_type == "audio/wav" and record.filename.startswith("discord-meeting-room-")
    assert _read(file_path(record).read_bytes())[0] == 16000


async def test_the_chat_is_asked_and_summary_only_sends_the_summary(client, user, bot, session_factory, redis):
    assert await _handoff(client, user, bot, session_factory, redis, 40, tap_label="📝 Summary") == "success"
    prompt = next(m for m in bot.sent if "What do you want" in m["text"])
    assert prompt["chat_id"] == CHAT and "#meeting-room" in prompt["text"]
    assert len(_notes(bot)) == 1 and "Ship Friday" in _notes(bot)[0]["text"] and _notes(bot)[0]["chat_id"] == CHAT
    assert not _documents(bot)


async def test_transcript_only_sends_the_whole_transcript_as_a_document(client, user, bot, session_factory, redis):
    assert await _handoff(client, user, bot, session_factory, redis, 40, tap_label="📄 Full transcript") == "success"
    (doc,) = _documents(bot)
    assert doc["chat_id"] == CHAT and doc["document"].startswith("transcript-meeting-room-") and doc["document"].endswith(".txt")
    assert doc["data"].decode() == TRANSCRIPT and doc["content_type"] == "text/plain"
    assert not _notes(bot)


async def test_both_sends_the_summary_and_the_transcript(client, user, bot, session_factory, redis):
    assert await _handoff(client, user, bot, session_factory, redis, 40, tap_label="Both") == "success"
    assert len(_notes(bot)) == 1 and len(_documents(bot)) == 1


async def test_no_answer_falls_back_to_the_summary(client, user, bot, session_factory, redis, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_CONFIRM_SECONDS", 1)
    assert await _handoff(client, user, bot, session_factory, redis, 40) == "success"
    assert len(_notes(bot)) == 1 and not _documents(bot)
    assert any("sending the summary" in e[2] for e in bot.edits)


async def test_a_pipeline_without_a_telegram_node_still_gets_its_result_from_the_bot(client, user, bot, session_factory, redis):
    assert await _handoff(client, user, bot, session_factory, redis, 40, tap_label="Both", graph=meeting_graph(sends=False)) == "success"
    assert len(_notes(bot)) == 1 and len(_documents(bot)) == 1


async def test_recordings_under_thirty_seconds_are_not_processed(client, user, bot, session_factory, redis, shared_session):
    assert await _handoff(client, user, bot, session_factory, redis, 10) == "too_short"
    assert "skipped the summary" in bot.texts()[-1]
    assert (await shared_session.scalars(select(WorkflowExecution))).first() is None
    assert not any("What do you want" in (t or "") for t in bot.texts())


async def test_a_missing_pipeline_is_reported_in_the_chat(client, user, bot, session_factory, redis):
    assert await _handoff(client, user, bot, session_factory, redis, 40, name="Something else") == "no_pipeline"
    assert "no “Meeting Notes” pipeline" in bot.texts()[-1]


async def test_a_failed_run_is_reported_in_the_chat_not_swallowed(client, user, bot, session_factory, redis):
    failing = meeting_graph()
    failing["nodes"].insert(1, {"id": "boom", "type": "http_request", "config": {"url": "http://127.0.0.1:9/unreachable"}})
    failing["edges"] = [{"source": "input", "target": "boom"}, {"source": "boom", "target": "send"}, {"source": "send", "target": "out"}]
    assert await _handoff(client, user, bot, session_factory, redis, 40, graph=failing) == "failed"
    assert bot.texts()[-1].startswith("❌ The meeting summary failed:") and "Run " in bot.texts()[-1]
    assert bot.sent[-1]["chat_id"] == CHAT


async def test_output_buttons_are_chat_bound(bot, redis, session_factory):
    center = CommandCenter(bot, session_factory=session_factory, redis=redis, task_queue=InlineQueue(session_factory, redis))
    task = asyncio.ensure_future(request_output_choice(bot, redis, CHAT, "meeting-room", 1.0, poll_seconds=0.01))
    for _ in range(200):
        if bot.sent:
            break
        await asyncio.sleep(0.01)
    buttons = {b["text"]: b["callback_data"] for b in bot.sent[0]["reply_markup"]["inline_keyboard"][0]}
    assert all(len(d.encode()) <= 64 for d in buttons.values())
    assert await center.handle_update(tap(uid(), buttons["Both"], chat=OTHER_CHAT)) == "invalid"
    assert await center.handle_update(tap(uid(), buttons["📄 Full transcript"])) == "voice_transcript"
    assert await task == "transcript"


# --- Audio-only output (the default) -----------------------------------------------------------


async def test_the_recording_is_sent_as_an_audio_file_with_no_summary_or_transcript(client, user, bot, session_factory):
    await create_workflow(client, user, meeting_graph(), name="Meeting Notes")
    wav = _wav(40)
    assert await send_recording(wav, 40, chat_id=CHAT, channel_name="meeting-room", bot=bot, session_factory=session_factory) == "sent"
    (doc,) = _documents(bot)
    assert doc["chat_id"] == CHAT and doc["content_type"] == "audio/mpeg" and doc["document"].endswith(".mp3")
    assert len(doc["data"]) > 1000 and len(doc["data"]) < len(wav)  # compressed, not the WAV
    assert "Recording of #meeting-room (0.7 min)" in doc["text"]
    assert len(bot.sent) == 1  # nothing else: no prompt, summary, or transcript


async def test_audio_mode_sends_even_when_no_pipeline_exists_and_skips_short_recordings(bot, session_factory):
    assert await send_recording(_wav(40), 40, chat_id=CHAT, channel_name="room", bot=bot, session_factory=session_factory) == "sent"
    assert await send_recording(_wav(5), 5, chat_id=CHAT, channel_name="room", bot=bot, session_factory=session_factory) == "too_short"
    assert "didn't send it" in bot.texts()[-1] and len(_documents(bot)) == 1


async def test_a_recording_over_telegrams_limit_is_reported_not_sent(bot, session_factory, monkeypatch):
    import app.services.discord_voice as dv

    monkeypatch.setattr(dv, "TELEGRAM_FILE_LIMIT", 1000)
    assert await send_recording(_wav(40), 40, chat_id=CHAT, channel_name="room", bot=bot, session_factory=session_factory) == "too_large"
    assert "over Telegram's 50 MB limit" in bot.texts()[-1] and not _documents(bot)



def test_frames_are_placed_by_rtp_timestamp_so_lost_frames_leave_gaps_not_jumps():
    clock = Clock()
    rec = MixedRecorder(clock)
    rec.start()
    frame = _tone(0.02, 3000)
    base = 4_294_966_000  # close to the 32-bit wrap
    for n in range(100):  # 2 s of speech, but frames 40-59 (0.4 s) never arrive
        clock.now += 0.02
        if 40 <= n < 60:
            continue
        rec.add(1, frame, (base + n * 960) % 2**32)
    rec.stop()
    samples = _read(rec.to_wav())[2]
    assert np.abs(samples[int(0.82 * 16000): int(1.18 * 16000)]).max() == 0  # the gap is silence, in the right place
    assert np.abs(samples[: int(0.7 * 16000)]).max() > 1000
    assert np.abs(samples[int(1.3 * 16000): int(1.9 * 16000)]).max() > 1000  # the rest was not pulled forward


def test_smooth_bridges_short_holes_fades_speech_edges_and_levels_the_peak():
    from app.services.discord_voice import smooth

    t = np.arange(16000)
    speech = (np.sin(2 * np.pi * 300 * t / 16000) * 3000).astype(np.int16)
    data = np.concatenate([np.zeros(4000, np.int16), speech, np.zeros(8000, np.int16), speech]).copy()
    data[4000 + 5000: 4000 + 5000 + 320] = 0  # a 20 ms hole from a lost frame, mid-speech
    out = smooth(data)
    assert np.all(out[4000 + 5000: 4000 + 5000 + 320] != 0) or np.abs(out[4000 + 5000: 4000 + 5000 + 320]).max() > 500  # bridged
    assert np.abs(out[4000 + 16000: 4000 + 16000 + 8000]).max() == 0  # a real 0.5 s pause stays silent
    assert abs(int(out[4000])) < 200 and abs(int(out[4000 + 16000 - 1])) < 200  # faded in and out: no clicks
    assert 17500 < np.abs(out).max() <= 18001  # raised by the 6x cap (3000 -> 18000), not past it
