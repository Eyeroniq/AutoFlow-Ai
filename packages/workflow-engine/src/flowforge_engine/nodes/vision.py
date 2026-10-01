"""Vision: ask Gemini about an uploaded image.

The image goes to Gemini inline with the prompt. PNG, JPEG, and WebP are sent as they are;
TIFF, BMP, and GIF (the other upload types) are converted to PNG first (a GIF's first
frame). With a `schema`, the reply must be JSON matching it: the same instruction,
parsing, validation, and single retry as Structured Output.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from flowforge_engine.errors import ProviderError
from flowforge_engine.files import IMAGE_TYPES, FileNotAvailable
from flowforge_engine.jsonschema_lite import check_schema
from flowforge_engine.models import GraphNode, NodeContext, NodeResult
from flowforge_engine.nodes.documents import FILE_FIELD, FileRef, _open_file
from flowforge_engine.nodes.structured import MAX_ATTEMPTS, check_reply, retry_request, schema_request
from flowforge_engine.registry import GuardedLLMConfig, NodeDefinition, register_node

# What Gemini takes inline; the rest are converted to PNG.
GEMINI_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
# Gemini's inline request limit is 20 MB (prompt included).
MAX_IMAGE_BYTES = 18 * 1024 * 1024


class VisionConfig(GuardedLLMConfig):
    image: FileRef = Field(
        description="The image: a reference like {{input.photo}} (an Input node of type file) or an uploaded file's id.",
        json_schema_extra=FILE_FIELD,
    )
    prompt: str = Field(min_length=1, description="What to do with the image, e.g. 'List every item and price on this receipt.'")
    schema_: dict[str, Any] | None = Field(
        default=None, alias="schema",
        description="Optional JSON Schema: the reply must be JSON matching it (validated, retried once), returned in `data`.",
    )
    provider: Literal["gemini"] = Field(default="gemini", description="Gemini (GEMINI_API_KEY).")
    model: str | None = Field(default=None, description="Blank = GEMINI_MODEL's default. Any Gemini model reads images.")
    system_prompt: str = ""
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=1, le=65_536)

    model_config = {"populate_by_name": True}

    @field_validator("schema_")
    @classmethod
    def _valid_schema(cls, schema: dict[str, Any] | None) -> dict[str, Any] | None:
        if schema is not None and (problems := check_schema(schema)):
            raise ValueError("; ".join(problems))
        return schema


class VisionResult(BaseModel):
    text: str
    data: Any = None
    attempts: int
    filename: str
    content_type: str
    converted: bool
    provider: str
    model: str
    mock: bool


def _to_png(path: Any) -> bytes:
    from PIL import Image

    with Image.open(path) as image:
        image.seek(0)
        out = io.BytesIO()
        image.convert("RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB").save(out, format="PNG")
        return out.getvalue()


@register_node("vision")
class VisionNode(NodeDefinition[VisionConfig]):
    category = "ai"
    guard_fields = ('prompt', 'system_prompt')
    label = "Vision"
    description = "Asks Gemini about an image: a description, extracted text, or JSON matching a schema."
    icon = "eye"
    config_schema = VisionConfig
    output_schema = VisionResult
    queue = "llm"

    def required_providers(self, node: GraphNode) -> list[tuple[str, str]]:
        return [("gemini", "provider")]

    async def execute(self, context: NodeContext, config: VisionConfig) -> NodeResult:
        try:
            stored = await _open_file(context, config.image, IMAGE_TYPES)
            provider = context.services.llm("gemini")
        except (FileNotAvailable, ProviderError) as exc:
            return NodeResult.fail(str(exc))
        converted = stored.content_type not in GEMINI_IMAGE_TYPES
        try:
            image = await asyncio.to_thread(_to_png, stored.path) if converted else stored.path.read_bytes()
        except Exception as exc:  # a corrupt file Pillow can't read
            return NodeResult.fail(f"Can't read '{stored.filename}' as an image: {type(exc).__name__}: {exc}")
        if len(image) > MAX_IMAGE_BYTES:
            return NodeResult.fail(f"'{stored.filename}' is {len(image) / 1048576:.1f} MB; Gemini takes images up to 18 MB inline")
        mime = "image/png" if converted else stored.content_type
        model = config.model or context.services.default_model("gemini")
        request = config.prompt.strip() if config.schema_ is None else schema_request(config.prompt.strip(), config.schema_)
        system = config.system_prompt.strip()
        if config.schema_ is not None:
            system = " ".join(filter(None, [system, "You reply with JSON only."]))
        meta = {"filename": stored.filename, "content_type": stored.content_type, "converted": converted,
                "provider": "gemini", "model": model, "mock": bool(getattr(provider, "is_mock", False))}

        problems: list[str] = []
        reply = ""
        for attempt in range(1, (MAX_ATTEMPTS if config.schema_ is not None else 1) + 1):
            prompt = request if attempt == 1 else retry_request(request, problems)
            try:
                reply = await provider.generate_with_image(system, prompt, image, mime, model, config.temperature, config.max_tokens)
            except ProviderError as exc:
                return NodeResult.fail(str(exc), attempts=attempt)
            if config.schema_ is None:
                return NodeResult.ok(text=reply, data=None, attempts=1, **meta)
            data, problems = check_reply(reply, config.schema_)
            if not problems:
                return NodeResult.ok(text=reply, data=data, attempts=attempt, **meta)
        return NodeResult.fail(
            f"The model's reply didn't match the schema after {MAX_ATTEMPTS} attempts: {'; '.join(problems[:5])}",
            attempts=MAX_ATTEMPTS, raw_reply=reply[:4000],
        )
