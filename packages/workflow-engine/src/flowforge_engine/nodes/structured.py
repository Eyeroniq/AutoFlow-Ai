"""Structured Output: an LLM reply as JSON that matches a JSON Schema.

The prompt carries the instructions and the input (usually a reference such as
{{stt.text}}); the schema is appended, the reply is parsed (code fences and prose around
the JSON are tolerated) and validated (flowforge_engine.jsonschema_lite). An invalid reply
is retried once with the problems listed; after that the node fails with them, so nothing
downstream reads a half-right object.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field, field_validator

from flowforge_engine.jsonschema_lite import check_schema, validate
from flowforge_engine.models import NodeContext, NodeResult
from flowforge_engine.nodes.ai import LLMChainFailed, generate_with_fallback
from flowforge_engine.nodes.documents import LLMChainConfig, _clip, _LLMDocumentNode
from flowforge_engine.nodes.lists import parse_json_reply
from flowforge_engine.registry import NodeDefinition, register_node

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2


def schema_request(prompt: str, schema: dict[str, Any]) -> str:
    """`prompt` plus the instruction to reply with JSON matching `schema` (also used by Vision)."""
    return (
        f"{prompt}\n\nReply with a single JSON value that matches this JSON Schema exactly, with no prose and no "
        f"code fences. Include every required field; use empty lists when there is nothing to list.\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )


def retry_request(request: str, problems: list[str]) -> str:
    return (
        f"{request}\n\nYour previous reply didn't match the schema: {'; '.join(problems[:10])}. "
        "Reply again with only the corrected JSON."
    )


def check_reply(reply: str, schema: dict[str, Any]) -> tuple[Any, list[str]]:
    """(parsed JSON, problems): problems is empty when the reply parses and matches `schema`."""
    try:
        data = parse_json_reply(reply)
    except ValueError as exc:
        return None, [str(exc)]
    return data, validate(data, schema)


class StructuredOutputConfig(LLMChainConfig):
    prompt: str = Field(min_length=1, description="Instructions and input, e.g. 'Write meeting notes from this transcript: {{stt.text}}'.")
    schema_: dict[str, Any] = Field(
        alias="schema",
        description='JSON Schema of the reply, e.g. {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}.',
    )
    system_prompt: str = Field(default="", description="Extra system instructions (the JSON-only rule is always added).")
    temperature: float = Field(default=0.2, ge=0, le=2)

    model_config = {"populate_by_name": True}

    @field_validator("schema_")
    @classmethod
    def _valid_schema(cls, schema: dict[str, Any]) -> dict[str, Any]:
        if problems := check_schema(schema):
            raise ValueError("; ".join(problems))
        return schema


class StructuredOutputResult(BaseModel):
    data: Any
    attempts: int
    input_chars: int
    truncated: bool
    provider: str
    provider_used: str
    model: str
    mock: bool
    fallback_errors: list[dict[str, Any]]


@register_node("structured_output")
class StructuredOutputNode(_LLMDocumentNode, NodeDefinition[StructuredOutputConfig]):
    category = "ai"
    guard_fields = ('prompt', 'system_prompt')
    label = "Structured Output"
    description = "Asks an LLM for JSON matching a schema, validates it, and retries once with the errors."
    icon = "braces"
    config_schema = StructuredOutputConfig
    output_schema = StructuredOutputResult
    queue = "llm"

    async def execute(self, context: NodeContext, config: StructuredOutputConfig) -> NodeResult:
        prompt, truncated = _clip(config.prompt.strip(), config.max_input_chars)
        request = schema_request(prompt, config.schema_)
        system = " ".join(filter(None, [config.system_prompt.strip(), "You reply with JSON only."]))
        fallback_errors: list[dict[str, Any]] = []
        problems: list[str] = []
        reply = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            user_prompt = request if attempt == 1 else retry_request(request, problems)
            try:
                answer = await generate_with_fallback(
                    context, provider=config.provider, model=config.model, fallback=config.fallback,
                    system_prompt=system, user_prompt=user_prompt, temperature=config.temperature, max_tokens=config.max_tokens,
                )
            except LLMChainFailed as exc:
                return NodeResult.fail(str(exc), fallback_errors=fallback_errors + exc.errors, attempts=attempt)
            fallback_errors += answer.fallback_errors
            reply = answer.text
            data, problems = check_reply(reply, config.schema_)
            if not problems:
                return NodeResult.ok(
                    data=data, attempts=attempt, input_chars=len(prompt), truncated=truncated,
                    provider=config.provider, provider_used=answer.provider_used, model=answer.model,
                    mock=answer.mock, fallback_errors=fallback_errors,
                )
            logger.warning("structured reply didn't match the schema", extra={
                "node_id": context.node_id, "attempt": attempt, "problems": problems[:5],
            })
        return NodeResult.fail(
            f"The model's reply didn't match the schema after {MAX_ATTEMPTS} attempts: {'; '.join(problems[:5])}",
            attempts=MAX_ATTEMPTS, raw_reply=reply[:4000], fallback_errors=fallback_errors,
        )
