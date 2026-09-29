"""Speech-to-text: chunk planning and timestamp stitching (pure), ffmpeg on generated audio
and video (probe, normalization, silence detection, cutting), the Speech to Text node with a
recording fake transcriber (so chunk boundaries and offsets can be checked exactly), and
the Groq Whisper adapter's requests and retries against an in-process transport."""

import shutil
import subprocess
import wave
from pathlib import Path

import httpx
import pytest

from flowforge_engine import (
    ExecutionServices,
    IssueCode,
    LocalFileStore,
    NodeStatus,
    ProviderSettings,
    WorkflowGraph,
    execute_node,
    get_node_definition,
    validate_workflow,
)
from flowforge_engine.media import format_timestamp, language_code, parse_silences, plan_chunks, stitch
from flowforge_engine.providers import RetryPolicy
from flowforge_engine.providers.transcription import GroqTranscriber
from flowforge_engine.testing import make_context, node

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


# --- planning and stitching -------------------------------------------------------------------


def test_short_files_are_one_chunk():
    assert plan_chunks(42.0, 600) == [(0.0, 42.0)]
    assert plan_chunks(600.0, 600) == [(0.0, 600.0)]


def test_without_silences_the_cuts_are_hard_and_cover_everything():
    assert plan_chunks(250.0, 100) == [(0.0, 100.0), (100.0, 200.0), (200.0, 250.0)]


def test_cuts_go_in_the_latest_silence_before_the_limit():
    silences = [(20.0, 21.0), (61.0, 63.0), (95.0, 96.0), (150.0, 152.0), (171.0, 173.0)]
    chunks = plan_chunks(260.0, 100, silences)
    # 95.5 (not 20.5: that's under half a chunk); then from 95.5 the latest in [145.5, 195.5] is 172.
    assert chunks == [(0.0, 95.5), (95.5, 172.0), (172.0, 260.0)]
    assert all(end - start <= 100 for start, end in chunks)
    assert all(a[1] == b[0] for a, b in zip(chunks, chunks[1:], strict=False))  # no gaps, no overlaps


def test_parse_silences_from_ffmpeg_output():
    log = """[silencedetect @ 0x55] silence_start: 16.9981
[silencedetect @ 0x55] silence_end: 20.0021 | silence_duration: 3.004
[silencedetect @ 0x55] silence_start: 37.1
[silencedetect @ 0x55] silence_end: 40 | silence_duration: 2.9
[silencedetect @ 0x55] silence_start: 49.5"""
    assert parse_silences(log) == [(16.9981, 20.0021), (37.1, 40.0)]  # a trailing unmatched start is dropped


def test_stitch_shifts_timestamps_and_keeps_order():
    pieces = [
        (0.0, 95.5, {"text": " Hello there. ", "language": "en",
                     "segments": [{"start": 0.0, "end": 2.5, "text": " Hello"}, {"start": 3.0, "end": 96.2, "text": "there."}]}),
        (95.5, 172.0, {"text": "Next part.", "language": "en", "segments": [{"start": 0.4, "end": 5.0, "text": "Next part."}]}),
        (172.0, 180.0, {"text": "", "language": "de", "segments": [{"start": 0.0, "end": 1.0, "text": "  "}]}),
        (180.0, 190.0, {"text": "Kein Segment.", "language": "de", "segments": []}),
    ]
    out = stitch(pieces)
    assert out["text"] == "Hello there. Next part. Kein Segment."
    assert out["segments"] == [
        {"start": 0.0, "end": 2.5, "text": "Hello", "id": 0},
        {"start": 3.0, "end": 95.5, "text": "there.", "id": 1},  # clamped to its chunk's end
        {"start": 95.9, "end": 100.5, "text": "Next part.", "id": 2},
        {"start": 180.0, "end": 190.0, "text": "Kein Segment.", "id": 3},  # a chunk without segments spans itself
    ]
    assert out["language"] == "en"  # 172 seconds of English, 18 of German


def test_language_names_and_timestamps():
    assert language_code("English") == "en" and language_code("en") == "en" and language_code("hindi") == "hi"
    assert language_code(None) is None and language_code("klingon") == "klingon"
    assert format_timestamp(3725.5) == "01:02:05.50"


# --- ffmpeg on generated media ---------------------------------------------------------------

# A 440 Hz tone with silence at 17-20 s and 37-40 s.
TONE_WITH_PAUSES = "aevalsrc='if(between(mod(t\\,20)\\,17\\,20)\\,0\\,0.5*sin(2*PI*440*t))':s=44100:d=50"


def make_audio(path: Path, expression: str = TONE_WITH_PAUSES) -> Path:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", expression,
                    "-ac", "2", str(path)], check=True)
    return path


def make_video(path: Path, *, audio: bool = True) -> Path:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:d=6"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=300:duration=6"]
    args += ["-shortest", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(args, check=True)
    return path


@needs_ffmpeg
async def test_probe_normalize_detect_and_cut(tmp_path):
    from flowforge_engine.media import cut, detect_silences, probe, to_wav

    source = make_audio(tmp_path / "talk.m4a")
    info = await probe(source)
    assert info.has_audio and not info.has_video and 49.5 < info.duration < 50.5
    wav = tmp_path / "norm.wav"
    await to_wav(source, wav)
    with wave.open(str(wav)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)
    silences = await detect_silences(wav)
    assert [(round(s), round(e)) for s, e in silences] == [(17, 20), (37, 40)]
    piece = tmp_path / "piece.mp3"
    await cut(wav, 18.5, 20.0, piece)
    assert 19.5 < (await probe(piece)).duration < 20.5


@needs_ffmpeg
async def test_video_audio_is_extracted_and_silent_video_is_refused(tmp_path):
    from flowforge_engine.media import probe

    assert (await probe(make_video(tmp_path / "clip.mp4"))).has_video
    store = LocalFileStore()
    silent = store.add(make_video(tmp_path / "silent.mp4", audio=False), content_type="video/mp4")
    result = await execute_node(node("stt", "speech_to_text", file=silent.id), make_context(services=services(store, groq=Recorder())))
    assert result.status is NodeStatus.FAILED and "has no audio track" in result.error


# --- the node ---------------------------------------------------------------------------------


class Recorder:
    """A transcriber that reports what it was sent: each chunk's duration, via ffprobe."""

    is_mock = False

    def __init__(self, max_upload_bytes=25 * 1024 * 1024, language="en"):
        self.max_upload_bytes = max_upload_bytes
        self.language = language
        self.calls: list[dict] = []

    async def transcribe(self, path, *, model, language=None, prompt=None, translate=False):
        from flowforge_engine.media import probe

        duration = (await probe(path)).duration
        index = len(self.calls)
        self.calls.append({"suffix": path.suffix, "duration": duration, "model": model, "language": language,
                           "prompt": prompt, "translate": translate})
        return {"text": f"Part {index}.", "language": self.language,
                "segments": [{"start": 1.0, "end": 2.0, "text": f"Part {index}."}]}


def services(store, **transcribers):
    return ExecutionServices(provider_settings=ProviderSettings(testing=True), files=store, transcribers=transcribers or None)


@needs_ffmpeg
async def test_long_audio_is_chunked_at_pauses_and_stitched(tmp_path):
    store = LocalFileStore()
    recording = store.add(make_audio(tmp_path / "meeting.wav"), content_type="audio/wav")
    groq = Recorder()
    result = await execute_node(
        node("stt", "speech_to_text", file=recording.id, chunk_minutes=0.5, prompt="FlowForge"),
        make_context(services=services(store, groq=groq)),
    )
    assert result.status is NodeStatus.SUCCESS, result.error
    out = result.output
    # 30 s chunks cut in the pauses at 18.5 s and 38.5 s: 0-18.5, 18.5-38.5, 38.5-50 (MP3 pads a little).
    assert [c["duration"] for c in groq.calls] == pytest.approx([18.5, 20.0, 11.5], abs=0.3)
    assert all(c["suffix"] == ".mp3" and c["model"] == "whisper-large-v3-turbo" and c["prompt"] == "FlowForge" for c in groq.calls)
    # Each chunk's segment at 1.0 s, shifted by where the chunk starts.
    assert [s["start"] for s in out["segments"]] == pytest.approx([1.0, 19.5, 39.5], abs=0.05)
    assert out["text"] == "Part 0. Part 1. Part 2."
    assert (out["chunks"], out["language"], out["language_name"], out["source"]) == (3, "en", "english", "audio")
    assert 49.5 < out["duration_seconds"] < 50.5


@needs_ffmpeg
async def test_the_upload_limit_bounds_the_chunk_length(tmp_path):
    store = LocalFileStore()
    recording = store.add(make_audio(tmp_path / "a.wav", "sine=frequency=500:duration=30"), content_type="audio/wav")
    small = Recorder(max_upload_bytes=45_000)  # 45 KB at 4 KB/s: under 11 s per chunk
    result = await execute_node(node("stt", "speech_to_text", file=recording.id), make_context(services=services(store, groq=small)))
    assert result.status is NodeStatus.SUCCESS, result.error
    assert len(small.calls) == 3 and all(c["duration"] <= 10.3 for c in small.calls)


@needs_ffmpeg
async def test_local_transcribes_in_one_pass_and_translation_picks_the_model(tmp_path):
    store = LocalFileStore()
    recording = store.add(make_video(tmp_path / "talk.webm"), content_type="video/webm")
    local = Recorder(max_upload_bytes=None, language="de")
    result = await execute_node(
        node("stt", "speech_to_text", file=recording.id, provider="local", task="translate"),
        make_context(services=services(store, local=local)),
    )
    assert result.status is NodeStatus.SUCCESS, result.error
    assert len(local.calls) == 1 and local.calls[0]["suffix"] == ".wav" and local.calls[0]["translate"] is True
    assert local.calls[0]["model"] == "base"  # FASTER_WHISPER_MODEL's default
    assert result.output["source"] == "video" and result.output["task"] == "translate"

    groq = Recorder()
    await execute_node(node("stt", "speech_to_text", file=recording.id, task="translate"), make_context(services=services(store, groq=groq)))
    assert groq.calls[0]["model"] == "whisper-large-v3" and groq.calls[0]["language"] is None


def test_config_rules_and_credentials():
    schema = get_node_definition("speech_to_text").config_schema
    with pytest.raises(ValueError, match="can't translate"):
        schema.model_validate({"file": "f", "task": "translate", "model": "whisper-large-v3-turbo"})
    graph = WorkflowGraph(nodes=[node("stt", "speech_to_text", file="f")])
    issues = validate_workflow(graph, services=ExecutionServices(provider_settings=ProviderSettings()))
    assert [i.code for i in issues] == [IssueCode.AUTH_MISSING] and "GROQ_API_KEY" in issues[0].message
    local = WorkflowGraph(nodes=[node("stt", "speech_to_text", file="f", provider="local")])
    assert validate_workflow(local, services=ExecutionServices(provider_settings=ProviderSettings())) == []


async def test_only_audio_and_video_are_accepted(tmp_path):
    store = LocalFileStore()
    (tmp_path / "x.pdf").write_bytes(b"%PDF-1.7")
    pdf = store.add(tmp_path / "x.pdf", content_type="application/pdf")
    result = await execute_node(node("stt", "speech_to_text", file=pdf.id), make_context(services=services(store, groq=Recorder())))
    assert result.status is NodeStatus.FAILED and "is application/pdf; this node reads audio/" in result.error


# --- the Groq adapter ---------------------------------------------------------------------------

KEY = "gsk_FakeGroqKeyForTests_0123456789abcdef"


class Groq:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    async def sleep(self, seconds):
        self.sleeps.append(seconds)

    def client(self, **kwargs):
        return GroqTranscriber(KEY, transport=httpx.MockTransport(self.handler), sleep=self.sleep,
                               retry=RetryPolicy(max_retries=2, base_delay=0.01, max_delay=30), **kwargs)


def form_fields(request: httpx.Request) -> dict[str, str]:
    body = request.content.decode("latin-1")
    fields = {}
    for part in body.split("--" + request.headers["content-type"].split("boundary=")[1]):
        if 'name="' in part and "filename=" not in part:
            name = part.split('name="')[1].split('"')[0]
            fields[name] = part.split("\r\n\r\n", 1)[1].rstrip("\r\n-")
    return fields


VERBOSE = {"task": "transcribe", "language": "english", "duration": 4.2, "text": " Hello world. ",
           "segments": [{"id": 0, "start": 0.0, "end": 1.8, "text": " Hello"}, {"id": 1, "start": 1.8, "end": 4.2, "text": " world."}]}


async def test_groq_transcription_request_and_parsing(tmp_path):
    audio = tmp_path / "chunk.mp3"
    audio.write_bytes(b"ID3fake")
    groq = Groq(httpx.Response(200, json=VERBOSE))
    result = await groq.client().transcribe(audio, model="whisper-large-v3-turbo", language="en", prompt="FlowForge")
    request = groq.requests[0]
    assert str(request.url) == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert form_fields(request) == {"model": "whisper-large-v3-turbo", "response_format": "verbose_json", "temperature": "0",
                                    "prompt": "FlowForge", "timestamp_granularities[]": "segment", "language": "en"}
    assert b'filename="chunk.mp3"' in request.content
    assert result == {"text": "Hello world.", "language": "en", "duration": 4.2,
                      "segments": [{"start": 0.0, "end": 1.8, "text": "Hello"}, {"start": 1.8, "end": 4.2, "text": "world."}]}


async def test_groq_drops_whispers_end_of_audio_hallucinations(tmp_path):
    # From a real response for samples/team-meeting.mp3 (100.3 s): the last window, past the
    # speech, came back as "Terima kasih" running to 130 s, with low confidence.
    audio = tmp_path / "chunk.mp3"
    audio.write_bytes(b"ID3fake")
    body = {"language": "english", "duration": 100.3, "text": " Thanks, everyone. Terima kasih",
            "segments": [
                {"start": 86.12, "end": 98.64, "text": " Thanks, everyone.", "avg_logprob": -0.14, "no_speech_prob": 0},
                {"start": 98.9, "end": 99.5, "text": " Hmm.", "avg_logprob": -1.3, "no_speech_prob": 0.9},  # rated silence
                {"start": 99.6, "end": 100.0, "text": " Right.", "avg_logprob": -1.2, "no_speech_prob": 0.1},  # kept: before the end
                {"start": 100.08, "end": 130.06, "text": " Terima kasih", "avg_logprob": -1.07, "no_speech_prob": 0},
            ]}
    result = await Groq(httpx.Response(200, json=body)).client().transcribe(audio, model="whisper-large-v3-turbo")
    assert [s["text"] for s in result["segments"]] == ["Thanks, everyone.", "Right."]
    assert result["text"] == "Thanks, everyone. Right."
    # A confident segment is never dropped, and ends are clamped to the audio.
    body["segments"][-1]["avg_logprob"] = -0.2
    result = await Groq(httpx.Response(200, json=body)).client().transcribe(audio, model="whisper-large-v3-turbo")
    assert result["segments"][-1] == {"start": 100.08, "end": 100.3, "text": "Terima kasih"}
    assert result["text"] == "Thanks, everyone. Right. Terima kasih"


async def test_groq_translation_endpoint_takes_no_language(tmp_path):
    audio = tmp_path / "c.mp3"
    audio.write_bytes(b"x")
    groq = Groq(httpx.Response(200, json={**VERBOSE, "task": "translate"}))
    await groq.client().transcribe(audio, model="whisper-large-v3", language="de", translate=True)
    request = groq.requests[0]
    assert request.url.path.endswith("/audio/translations")
    assert "language" not in form_fields(request) and "timestamp_granularities[]" not in form_fields(request)


async def test_groq_429_waits_for_retry_after_and_errors_hide_the_key(tmp_path):
    audio = tmp_path / "c.mp3"
    audio.write_bytes(b"x")
    groq = Groq(httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "Rate limit reached"}}),
                httpx.Response(200, json=VERBOSE))
    await groq.client().transcribe(audio, model="whisper-large-v3-turbo")
    assert len(groq.requests) == 2 and 7 <= groq.sleeps[0] <= 7.25

    too_long = Groq(httpx.Response(429, headers={"retry-after": "900"}, json={"error": {"message": "audio seconds per hour"}}))
    with pytest.raises(Exception, match="asked to wait 900s") as caught:
        await too_long.client().transcribe(audio, model="whisper-large-v3-turbo")
    assert "7,200 audio seconds an hour" in str(caught.value)

    bad_key = Groq(httpx.Response(401, json={"error": {"message": f"Invalid API Key {KEY}"}}))
    with pytest.raises(Exception, match="authentication failed") as caught:
        await bad_key.client().transcribe(audio, model="whisper-large-v3-turbo")
    assert KEY not in str(caught.value)


async def test_groq_verify_lists_the_whisper_models():
    groq = Groq(httpx.Response(200, json={"data": [{"id": "whisper-large-v3"}, {"id": "llama"}, {"id": "whisper-large-v3-turbo"}]}))
    assert await groq.client().verify() == {"whisper_models": ["whisper-large-v3", "whisper-large-v3-turbo"]}


def test_upload_limit_follows_the_setting():
    assert GroqTranscriber(KEY, max_file_mb=25).max_upload_bytes == 25 * 1024 * 1024
    assert GroqTranscriber(KEY, max_file_mb=100).max_upload_bytes == 100 * 1024 * 1024
