"""Speech-to-Text: audio and video files to a timestamped transcript.

The file (mp3, wav, m4a, mp4, webm, ogg, flac, mov) is probed and normalized with ffmpeg to
16 kHz mono (video is dropped). With Groq, the audio is sent in chunks under the upload
limit, cut in silences where possible, and the chunk transcripts are stitched with their
timestamps shifted back into place (flowforge_engine.media). Local faster-whisper reads
the whole file. Runs on the "audio" queue (worker-audio).

There are no speaker labels: Whisper doesn't identify speakers, so none are invented.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from flowforge_engine.errors import ProviderError
from flowforge_engine.files import MEDIA_TYPES, VIDEO_TYPES, FileNotAvailable
from flowforge_engine.media import (
    CHUNK_BYTES_PER_SECOND,
    WHISPER_LANGUAGES,
    MediaError,
    cut,
    detect_silences,
    language_code,
    plan_chunks,
    probe,
    stitch,
    to_wav,
)
from flowforge_engine.models import GraphNode, NodeContext, NodeResult
from flowforge_engine.nodes.documents import FILE_FIELD, FileRef, _open_file
from flowforge_engine.registry import NodeConfig, NodeDefinition, register_node

logger = logging.getLogger(__name__)

TURBO = "whisper-large-v3-turbo"
MEDIA_DESCRIPTION = "The recording: a reference like {{input.recording}} (an Input node of type file) or an uploaded file's id."


class SpeechToTextConfig(NodeConfig):
    file: FileRef = Field(description=MEDIA_DESCRIPTION, json_schema_extra=FILE_FIELD)
    provider: Literal["groq", "local"] = Field(
        default="groq",
        description="groq: Groq's Whisper API (GROQ_API_KEY; free tier). local: faster-whisper on the worker's CPU (no key; the model downloads on first use).",
    )
    model: str | None = Field(
        default=None,
        description="Blank: whisper-large-v3-turbo on Groq (whisper-large-v3 when translating), or the FASTER_WHISPER_MODEL size locally (tiny, base, small, medium, large-v3, turbo).",
    )
    task: Literal["transcribe", "translate"] = Field(
        default="transcribe", description="translate: an English transcript of speech in any language."
    )
    language: str | None = Field(
        default=None, pattern=r"^[a-z]{2,3}$", description="The spoken language as a code (en, de, hi, ...). Blank: detected."
    )
    prompt: str = Field(default="", description="Optional: names and terms to spell right, e.g. 'FlowForge, Celery, Kubernetes'.")
    chunk_minutes: float = Field(
        default=10, ge=0.25, le=60,
        description="Groq: longest piece sent at once (also kept under the upload limit). Pieces are cut in pauses where possible.",
    )
    max_duration_minutes: float = Field(default=240, gt=0, le=1440, description="Refuse longer recordings.")
    timeout_seconds: float = Field(default=900, gt=0, le=3600, description="The node's time budget.")

    @model_validator(mode="after")
    def _translation_model(self) -> Self:
        if self.provider == "groq" and self.task == "translate" and self.model == TURBO:
            raise ValueError(f"{TURBO} can't translate; use whisper-large-v3 (or leave the model blank)")
        return self


class Segment(BaseModel):
    id: int
    start: float
    end: float
    text: str


class SpeechToTextResult(BaseModel):
    text: str
    segments: list[Segment]
    language: str | None
    language_name: str | None
    duration_seconds: float
    chunks: int
    provider: str
    model: str
    task: str
    source: Literal["audio", "video"]
    filename: str
    mock: bool


@register_node("speech_to_text")
class SpeechToTextNode(NodeDefinition[SpeechToTextConfig]):
    category = "audio"
    label = "Speech to Text"
    description = "Transcribes audio or video (Groq Whisper or local faster-whisper) with timestamps; long files are chunked."
    icon = "audio-lines"
    config_schema = SpeechToTextConfig
    output_schema = SpeechToTextResult
    queue = "audio"

    def required_providers(self, node: GraphNode) -> list[tuple[str, str]]:
        return [("groq", "provider")] if node.config.get("provider", "groq") == "groq" else []

    def timeout(self, config: SpeechToTextConfig, default: float) -> float:
        return config.timeout_seconds

    async def execute(self, context: NodeContext, config: SpeechToTextConfig) -> NodeResult:
        try:
            stored = await _open_file(context, config.file, MEDIA_TYPES)
            transcriber = context.services.transcriber(config.provider)
        except (FileNotAvailable, ProviderError) as exc:
            return NodeResult.fail(str(exc))
        speech = context.services.settings.speech
        translate = config.task == "translate"
        if config.provider == "groq":
            model = config.model or (speech.groq_translate_model if translate else speech.groq_model)
        else:
            model = config.model or speech.local_model

        try:
            with tempfile.TemporaryDirectory(prefix="flowforge-stt-") as tmp:
                work = Path(tmp)
                info = await probe(stored.path)
                if not info.has_audio:
                    return NodeResult.fail(f"'{stored.filename}' has no audio track")
                if info.duration > config.max_duration_minutes * 60:
                    return NodeResult.fail(
                        f"'{stored.filename}' is {info.duration / 60:.0f} minutes; max_duration_minutes is {config.max_duration_minutes:g}"
                    )
                wav = work / "audio.wav"
                await to_wav(stored.path, wav)
                # 16 kHz, 16-bit mono: 32,000 bytes a second after the 44-byte header.
                duration = info.duration or max(0.0, (wav.stat().st_size - 44) / 32000)
                pieces = await self._transcribe(transcriber, wav, work, duration, config, model, translate)
        except MediaError as exc:
            return NodeResult.fail(f"'{stored.filename}': {exc}")
        except ProviderError as exc:
            return NodeResult.fail(str(exc))

        stitched = stitch(pieces)
        language = config.language if (config.language and not translate) else language_code(stitched["language"])
        logger.info("transcribed", extra={
            "node_id": context.node_id, "provider": config.provider, "model": model, "seconds": round(duration, 1),
            "chunks": len(pieces), "segments": len(stitched["segments"]),
        })
        return NodeResult.ok(
            text=stitched["text"],
            segments=stitched["segments"],
            language=language,
            language_name=WHISPER_LANGUAGES.get(language or "", None),
            duration_seconds=round(duration, 2),
            chunks=len(pieces),
            provider=config.provider,
            model=model,
            task=config.task,
            source="video" if stored.content_type in VIDEO_TYPES or info.has_video else "audio",
            filename=stored.filename,
            mock=bool(getattr(transcriber, "is_mock", False)),
        )

    async def _transcribe(
        self, transcriber: Any, wav: Path, work: Path, duration: float, config: SpeechToTextConfig, model: str, translate: bool
    ) -> list[tuple[float, float, dict[str, Any]]]:
        request = {"model": model, "language": None if translate else config.language, "prompt": config.prompt or None,
                   "translate": translate}
        limit = getattr(transcriber, "max_upload_bytes", None)
        if limit is None:  # no upload limit: one pass over the whole file
            return [(0.0, duration, await transcriber.transcribe(wav, **request))]
        max_seconds = min(config.chunk_minutes * 60, limit * 0.9 / CHUNK_BYTES_PER_SECOND)
        silences = await detect_silences(wav) if duration > max_seconds else []
        pieces = []
        for index, (start, end) in enumerate(plan_chunks(duration, max_seconds, silences)):
            chunk = work / f"chunk-{index:03d}.mp3"
            await cut(wav, start, end - start, chunk)
            pieces.append((start, end, await transcriber.transcribe(chunk, **request)))
            chunk.unlink(missing_ok=True)
        return pieces


class DiscordVoiceConfig(NodeConfig):
    guild_id: str = Field(
        default="", max_length=32, pattern=r"^\d*$",
        description="The Discord server (guild) id. Discord: Settings > Advanced > Developer Mode, then right-click the server > Copy Server ID.",
    )
    channel_id: str = Field(
        default="", max_length=32, pattern=r"^\d*$",
        description="The voice channel id to watch (right-click the channel > Copy Channel ID).",
    )
    max_minutes: int = Field(default=90, ge=1, le=240, description="Stop recording after this many minutes.")


class DiscordVoiceResult(BaseModel):
    source: str
    guild_id: str
    channel_id: str
    max_minutes: int


@register_node("discord_voice")
class DiscordVoiceNode(NodeDefinition[DiscordVoiceConfig]):
    """The trigger block for Discord meetings. The always-on Discord bot reads this block's settings: it watches the
    channel, asks in Telegram before recording, and runs this pipeline with the recording as its file Input."""

    category = "sources"
    label = "Discord Voice Meeting"
    description = (
        "Starts this pipeline from a Discord voice channel: after you approve in Telegram, the bot records the meeting "
        "and passes the audio to the pipeline's File input."
    )
    icon = "audio-lines"
    config_schema = DiscordVoiceConfig
    output_schema = DiscordVoiceResult

    async def execute(self, context: NodeContext, config: DiscordVoiceConfig) -> NodeResult:
        return NodeResult.ok(
            source="discord_voice", guild_id=config.guild_id, channel_id=config.channel_id, max_minutes=config.max_minutes
        )
