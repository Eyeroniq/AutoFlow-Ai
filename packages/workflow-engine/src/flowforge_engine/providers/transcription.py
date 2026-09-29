"""Speech-to-text providers: Groq's Whisper API and local faster-whisper.

Both take an audio file already normalized by flowforge_engine.media and return
{"text", "segments": [{"start", "end", "text"}], "language"} with timestamps relative to
that file. Neither labels speakers (Whisper doesn't identify them, and nothing here pretends
to).

- **Groq** (https://console.groq.com/docs/speech-to-text): POST /openai/v1/audio/
  transcriptions (or /translations, to English) with `response_format=verbose_json`. Uploads
  are limited (25 MB on the free tier), so the node sends chunks; `max_upload_bytes` says how
  big one may be. A 429 is retried after its `retry-after`.
- **Local** (faster-whisper, CPU, int8): the model is downloaded from Hugging Face on first
  use into `models_dir` (a volume in Docker) and kept loaded per worker process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from flowforge_engine.errors import ProviderError
from flowforge_engine.media import language_code
from flowforge_engine.providers.retry import RetryPolicy, classify_status, parse_retry_after, redact, with_retries

logger = logging.getLogger(__name__)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# Whisper's prompt is limited to 224 tokens; keep well under it.
MAX_PROMPT_CHARS = 800


# Whisper's own thresholds (openai-whisper's transcribe(): logprob_threshold, no_speech_threshold).
LOGPROB_THRESHOLD = -1.0
NO_SPEECH_THRESHOLD = 0.6


def _is_hallucination(segment: dict[str, Any], duration: float | None) -> bool:
    """Whisper invents text for silence, most often in the window past the end of the audio
    ("Thank you", "Terima kasih"). A segment is dropped when it is low-confidence
    (avg_logprob < -1) and either Whisper itself rates it as probably silence or it runs to or
    past the end of the audio."""
    logprob = segment.get("avg_logprob")
    if logprob is None or float(logprob) >= LOGPROB_THRESHOLD:
        return False
    if float(segment.get("no_speech_prob") or 0.0) > NO_SPEECH_THRESHOLD:
        return True
    return duration is not None and float(segment.get("end") or 0.0) >= duration - 0.05


def _segments(raw: list[dict[str, Any]] | None, duration: float | None = None) -> list[dict[str, Any]]:
    segments = []
    for s in raw or []:
        if _is_hallucination(s, duration):
            logger.info("dropped a low-confidence Whisper segment", extra={"start": s.get("start"), "text": s.get("text")})
            continue
        end = float(s.get("end") or 0.0)
        segments.append({
            "start": float(s.get("start") or 0.0),
            "end": min(end, duration) if duration else end,
            "text": str(s.get("text") or "").strip(),
        })
    return segments


class GroqTranscriber:
    is_mock = False

    def __init__(
        self,
        api_key: str,
        *,
        name: str = "groq",
        retry: RetryPolicy | None = None,
        timeout: float = 300,
        max_file_mb: float = 25,
        base_url: str = GROQ_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.name = name
        self._key = api_key
        self._retry = retry or RetryPolicy()
        self._timeout = timeout
        self._base = base_url.rstrip("/")
        self._transport = transport
        self._sleep = sleep
        self.max_upload_bytes: int | None = int(max_file_mb * 1024 * 1024)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"}

    async def _once(self, endpoint: str, path: Path, data: dict[str, str]) -> dict[str, Any]:
        content = path.read_bytes()
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.post(
                    f"{self._base}{endpoint}", headers=self._headers(), data=data,
                    files={"file": (path.name, content, "audio/mpeg" if path.suffix == ".mp3" else "audio/wav")},
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, f"no transcription within {self._timeout:g}s", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"network error: {type(exc).__name__}: {redact(str(exc), self._key)}", retryable=True) from exc
        if response.status_code >= 400:
            try:
                body = response.json()
                message = str((body.get("error") or {}).get("message") or body)
            except ValueError:
                message = response.text[:300]
            message = redact(message, self._key)
            if response.status_code == 413:
                raise ProviderError(self.name, f"the audio chunk is over Groq's upload limit (HTTP 413): {message}", status_code=413)
            error = classify_status(self.name, response.status_code, message, retry_after=parse_retry_after(response.headers))
            if response.status_code == 429:
                error.args = (f"{error.args[0]} (the free tier allows 20 requests a minute and 7,200 audio seconds an hour)",)
            raise error
        try:
            return response.json()
        except ValueError:
            raise ProviderError(self.name, "the transcription response wasn't JSON") from None

    async def transcribe(
        self, path: Path, *, model: str, language: str | None = None, prompt: str | None = None, translate: bool = False
    ) -> dict[str, Any]:
        data = {"model": model, "response_format": "verbose_json", "temperature": "0"}
        if prompt:
            data["prompt"] = prompt[:MAX_PROMPT_CHARS]
        if translate:
            endpoint = "/audio/translations"
        else:
            endpoint = "/audio/transcriptions"
            data["timestamp_granularities[]"] = "segment"
            if language:
                data["language"] = language
        body = await with_retries(lambda: self._once(endpoint, path, data), self._retry, sleep=self._sleep)
        duration = float(body["duration"]) if body.get("duration") else None
        segments = _segments(body.get("segments"), duration)
        text = str(body.get("text") or "").strip()
        if body.get("segments") and len(segments) < len(body["segments"]):
            text = " ".join(s["text"] for s in segments if s["text"])
        return {
            "text": text,
            "segments": segments,
            "language": language_code(body.get("language")),
            "duration": body.get("duration"),
        }

    async def verify(self) -> dict[str, Any]:
        """The key can list models, and the Whisper models are among them (nothing is transcribed)."""
        async def list_models() -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
                response = await client.get(f"{self._base}/models", headers=self._headers())
            if response.status_code >= 400:
                raise classify_status(self.name, response.status_code, redact(response.text[:300], self._key))
            return response.json()

        models = await with_retries(list_models, self._retry, sleep=self._sleep)
        whisper = sorted(m.get("id") for m in models.get("data", []) if "whisper" in str(m.get("id")))
        return {"whisper_models": whisper}


# --- local faster-whisper ----------------------------------------------------------------------

_models: dict[tuple[str, str, str], Any] = {}
_models_lock = threading.Lock()


class _Cancelled(Exception):
    pass


def _load_model(size: str, models_dir: str, compute_type: str) -> Any:
    key = (size, models_dir, compute_type)
    with _models_lock:
        if key not in _models:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise ProviderError("local", "faster-whisper isn't installed on this worker") from exc
            os.makedirs(models_dir, exist_ok=True)
            logger.info("loading faster-whisper model (downloaded on first use)", extra={"model": size, "dir": models_dir})
            try:
                _models[key] = WhisperModel(size, device="cpu", compute_type=compute_type, download_root=models_dir)
            except Exception as exc:  # unknown size, no network for the first download, disk full
                raise ProviderError("local", f"can't load faster-whisper model '{size}': {type(exc).__name__}: {exc}") from exc
        return _models[key]


class LocalWhisperTranscriber:
    """faster-whisper on the worker's CPU. No upload limit, so files aren't chunked."""

    is_mock = False
    max_upload_bytes: int | None = None

    def __init__(self, *, model: str = "base", models_dir: str | None = None, compute_type: str = "int8", name: str = "local"):
        self.name = name
        self.model = model
        self.models_dir = models_dir or os.path.join(os.path.expanduser("~"), ".cache", "flowforge-whisper")
        self.compute_type = compute_type

    async def transcribe(
        self, path: Path, *, model: str, language: str | None = None, prompt: str | None = None, translate: bool = False
    ) -> dict[str, Any]:
        cancel = threading.Event()

        def work() -> dict[str, Any]:
            whisper = _load_model(model or self.model, self.models_dir, self.compute_type)
            segments, info = whisper.transcribe(
                str(path), task="translate" if translate else "transcribe", language=language or None,
                initial_prompt=(prompt or None) and prompt[:MAX_PROMPT_CHARS], vad_filter=True, beam_size=5,
            )
            collected = []
            for segment in segments:  # decoding happens as this generator is consumed
                if cancel.is_set():
                    raise _Cancelled
                raw = {"end": segment.end, "avg_logprob": segment.avg_logprob, "no_speech_prob": segment.no_speech_prob}
                if _is_hallucination(raw, info.duration):
                    continue
                collected.append({"start": float(segment.start), "end": min(float(segment.end), info.duration), "text": segment.text.strip()})
            return {
                "text": " ".join(s["text"] for s in collected if s["text"]),
                "segments": collected,
                "language": language_code(info.language),
                "language_probability": round(float(info.language_probability), 3),
                "duration": info.duration,
            }

        try:
            return await asyncio.to_thread(work)
        except asyncio.CancelledError:
            cancel.set()
            raise

    async def verify(self) -> dict[str, Any]:
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise ProviderError(self.name, "faster-whisper isn't installed on this worker") from exc
        cached = sorted(p.name for p in Path(self.models_dir).glob("models--*")) if Path(self.models_dir).is_dir() else []
        return {"model": self.model, "models_dir": self.models_dir, "downloaded": cached}


class MockTranscriber:
    """Canned transcripts (the test suite and provider "mock"); no audio is sent anywhere."""

    is_mock = True
    max_upload_bytes: int | None = None

    def __init__(self, name: str = "mock"):
        self.name = name

    async def transcribe(
        self, path: Path, *, model: str, language: str | None = None, prompt: str | None = None, translate: bool = False
    ) -> dict[str, Any]:
        text = f"[MOCK TRANSCRIPT of {path.name}]"
        return {"text": text, "segments": [{"start": 0.0, "end": 1.0, "text": text}], "language": language or "en"}

    async def verify(self) -> dict[str, Any]:
        return {"mock": True}
