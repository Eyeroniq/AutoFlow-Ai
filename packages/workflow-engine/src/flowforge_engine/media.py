"""Audio and video preparation for speech-to-text, with ffmpeg and ffprobe.

Every recording is first normalized to 16 kHz mono PCM (what Whisper models listen to;
video streams are dropped), then, for providers with an upload limit, split into chunks.
Cuts are placed in silences near each chunk's target length where there are any, so words
aren't split, and each chunk's timestamps are shifted by where it starts when the
transcripts are stitched back together.

The pure parts (`parse_silences`, `plan_chunks`, `stitch`) are unit tested; the ffmpeg calls
run as subprocesses that are killed if the node is cancelled.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
# Speech is well served by 16 kHz mono at 32 kbit/s MP3: about 4 KB per second.
CHUNK_BITRATE = "32k"
CHUNK_BYTES_PER_SECOND = 4000

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


class MediaError(Exception):
    """The file can't be read or converted; the message is safe to show."""


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    has_audio: bool
    has_video: bool
    format_name: str


async def run_tool(*args: str, timeout: float = 600) -> tuple[int, str]:
    """Run ffmpeg/ffprobe; returns (exit code, stderr+stdout). Killed on cancel or timeout."""
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        raise MediaError(f"{args[0]} isn't installed on this worker (the worker image installs ffmpeg)") from None
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (asyncio.CancelledError, TimeoutError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        raise
    return process.returncode or 0, (stdout.decode("utf-8", "replace") + stderr.decode("utf-8", "replace"))


def _tail(output: str, lines: int = 3) -> str:
    kept = [line.strip() for line in output.strip().splitlines() if line.strip()]
    return " | ".join(kept[-lines:])[:500]


async def probe(path: Path) -> MediaInfo:
    code, output = await run_tool(
        FFPROBE, "-v", "error", "-show_entries", "format=duration,format_name:stream=codec_type",
        "-of", "json", str(path), timeout=60,
    )
    if code != 0:
        raise MediaError(f"ffprobe can't read the file: {_tail(output)}")
    try:
        data = json.loads(output[output.index("{"):])
    except (ValueError, json.JSONDecodeError):
        raise MediaError("ffprobe returned no information about the file") from None
    kinds = {stream.get("codec_type") for stream in data.get("streams", [])}
    fmt = data.get("format", {})
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        duration = 0.0
    return MediaInfo(duration=duration, has_audio="audio" in kinds, has_video="video" in kinds,
                     format_name=str(fmt.get("format_name", "")))


async def to_wav(source: Path, target: Path) -> None:
    """16 kHz mono 16-bit PCM WAV, without any video."""
    code, output = await run_tool(
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target), timeout=1800,
    )
    if code != 0 or not target.is_file() or target.stat().st_size <= 44:
        raise MediaError(f"ffmpeg couldn't extract the audio: {_tail(output) or 'no audio decoded'}")


async def cut(source: Path, start: float, duration: float, target: Path) -> None:
    """A piece of a (normalized) recording as 16 kHz mono MP3."""
    code, output = await run_tool(
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
        "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", CHUNK_BITRATE, str(target), timeout=600,
    )
    if code != 0 or not target.is_file():
        raise MediaError(f"ffmpeg couldn't cut the audio at {start:.1f}s: {_tail(output)}")


async def detect_silences(path: Path, *, noise_db: int = -35, min_seconds: float = 0.35) -> list[tuple[float, float]]:
    code, output = await run_tool(
        FFMPEG, "-nostdin", "-hide_banner", "-nostats", "-i", str(path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_seconds}", "-f", "null", "-", timeout=1800,
    )
    if code != 0:
        return []  # chunking falls back to fixed cut points
    return parse_silences(output)


def parse_silences(output: str) -> list[tuple[float, float]]:
    """(start, end) pairs from ffmpeg's silencedetect log lines."""
    silences: list[tuple[float, float]] = []
    start: float | None = None
    for line in output.splitlines():
        if match := _SILENCE_START.search(line):
            start = max(0.0, float(match.group(1)))
        elif (match := _SILENCE_END.search(line)) and start is not None:
            silences.append((start, float(match.group(1))))
            start = None
    return silences


def plan_chunks(
    duration: float,
    max_seconds: float,
    silences: list[tuple[float, float]] = (),  # type: ignore[assignment]
    *,
    min_fraction: float = 0.5,
) -> list[tuple[float, float]]:
    """(start, end) pieces of at most `max_seconds` covering [0, duration] without gaps.

    Each cut goes in the middle of the latest silence between `min_fraction` of a chunk and
    its full length after the previous cut; with no silence there, it's a hard cut at the
    full length.
    """
    if max_seconds <= 0:
        raise ValueError("max_seconds must be positive")
    if duration <= max_seconds:
        return [(0.0, max(duration, 0.0))]
    midpoints = sorted((s + e) / 2 for s, e in silences if e > s)
    chunks: list[tuple[float, float]] = []
    start = 0.0
    while duration - start > max_seconds:
        earliest, latest = start + max_seconds * min_fraction, start + max_seconds
        candidates = [m for m in midpoints if earliest <= m <= latest]
        cut_at = candidates[-1] if candidates else latest
        chunks.append((round(start, 3), round(cut_at, 3)))
        start = cut_at
    chunks.append((round(start, 3), round(duration, 3)))
    return chunks


def stitch(pieces: list[tuple[float, float, dict[str, Any]]]) -> dict[str, Any]:
    """One transcript from per-chunk results: (chunk start, chunk end, {text, segments,
    language}). Segment times are shifted by the chunk's start and kept inside the chunk;
    the language is the one detected over the most audio."""
    segments: list[dict[str, Any]] = []
    texts: list[str] = []
    seconds_by_language: dict[str, float] = {}
    for offset, end, result in pieces:
        text = (result.get("text") or "").strip()
        if text:
            texts.append(text)
        language = result.get("language")
        if language:
            seconds_by_language[language] = seconds_by_language.get(language, 0.0) + (end - offset)
        chunk_segments = result.get("segments") or ([{"start": 0.0, "end": end - offset, "text": text}] if text else [])
        for segment in chunk_segments:
            words = (segment.get("text") or "").strip()
            if not words:
                continue
            seg_start = min(offset + max(0.0, float(segment.get("start") or 0.0)), end)
            seg_end = min(offset + max(0.0, float(segment.get("end") or 0.0)), end)
            segments.append({"start": round(seg_start, 2), "end": round(max(seg_end, seg_start), 2), "text": words})
    for index, segment in enumerate(segments):
        segment["id"] = index
    language = max(seconds_by_language, key=seconds_by_language.__getitem__) if seconds_by_language else None
    return {"text": " ".join(texts), "segments": segments, "language": language}


def format_timestamp(seconds: float) -> str:
    """12.5 -> "00:00:12.50"."""
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}.{int(round((seconds - whole) * 100)) % 100:02d}"


# Whisper reports languages by name ("english") on the OpenAI-style APIs and by code ("en")
# in faster-whisper; outputs use the code.
WHISPER_LANGUAGES = {
    "en": "english", "zh": "chinese", "de": "german", "es": "spanish", "ru": "russian", "ko": "korean",
    "fr": "french", "ja": "japanese", "pt": "portuguese", "tr": "turkish", "pl": "polish", "ca": "catalan",
    "nl": "dutch", "ar": "arabic", "sv": "swedish", "it": "italian", "id": "indonesian", "hi": "hindi",
    "fi": "finnish", "vi": "vietnamese", "he": "hebrew", "uk": "ukrainian", "el": "greek", "ms": "malay",
    "cs": "czech", "ro": "romanian", "da": "danish", "hu": "hungarian", "ta": "tamil", "no": "norwegian",
    "th": "thai", "ur": "urdu", "hr": "croatian", "bg": "bulgarian", "lt": "lithuanian", "la": "latin",
    "mi": "maori", "ml": "malayalam", "cy": "welsh", "sk": "slovak", "te": "telugu", "fa": "persian",
    "lv": "latvian", "bn": "bengali", "sr": "serbian", "az": "azerbaijani", "sl": "slovenian", "kn": "kannada",
    "et": "estonian", "mk": "macedonian", "br": "breton", "eu": "basque", "is": "icelandic", "hy": "armenian",
    "ne": "nepali", "mn": "mongolian", "bs": "bosnian", "kk": "kazakh", "sq": "albanian", "sw": "swahili",
    "gl": "galician", "mr": "marathi", "pa": "punjabi", "si": "sinhala", "km": "khmer", "sn": "shona",
    "yo": "yoruba", "so": "somali", "af": "afrikaans", "oc": "occitan", "ka": "georgian", "be": "belarusian",
    "tg": "tajik", "sd": "sindhi", "gu": "gujarati", "am": "amharic", "yi": "yiddish", "lo": "lao",
    "uz": "uzbek", "fo": "faroese", "ht": "haitian creole", "ps": "pashto", "tk": "turkmen", "nn": "nynorsk",
    "mt": "maltese", "sa": "sanskrit", "lb": "luxembourgish", "my": "myanmar", "bo": "tibetan", "tl": "tagalog",
    "mg": "malagasy", "as": "assamese", "tt": "tatar", "haw": "hawaiian", "ln": "lingala", "ha": "hausa",
    "ba": "bashkir", "jw": "javanese", "su": "sundanese", "yue": "cantonese",
}
_CODES = {name: code for code, name in WHISPER_LANGUAGES.items()}


def language_code(value: str | None) -> str | None:
    """"English" / "english" / "en" -> "en"; unknown values are returned lowercased."""
    if not value:
        return None
    text = value.strip().lower()
    return text if text in WHISPER_LANGUAGES else _CODES.get(text, text)
