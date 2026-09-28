from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, field_validator

from flowforge_engine.errors import ProviderError
from flowforge_engine.models import GraphNode, NodeContext, NodeResult
from flowforge_engine.providers.settings import LLM_PROVIDER_NAMES, LLMProviderName
from flowforge_engine.registry import NodeConfig, NodeDefinition, register_node
from flowforge_engine.variables import contains_reference

logger = logging.getLogger(__name__)

MAX_FALLBACKS = 5


def parse_chain_entry(entry: str) -> tuple[str, str | None]:
    """"groq" -> ("groq", None); "openrouter:google/gemma-4-31b-it:free" -> ("openrouter", "google/...:free")."""
    provider, _, model = entry.strip().partition(":")
    return provider.strip().lower(), (model.strip() or None)


class LLMConfig(NodeConfig):
    provider: LLMProviderName = Field(description="Which LLM service answers. 'mock' returns a canned reply.")
    model: str | None = Field(
        default=None,
        description="Model id. Blank uses the provider's default (e.g. GEMINI_MODEL / GROQ_MODEL).",
    )
    system_prompt: str = ""
    user_prompt: str = Field(min_length=1)
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=1, le=65536)
    fallback: list[str] = Field(
        default_factory=list,
        max_length=MAX_FALLBACKS,
        description=(
            "Providers to try in order if this one fails, each 'provider' or 'provider:model', "
            'e.g. ["groq", "openrouter:openrouter/free", "ollama"]. The answering provider is '
            "reported as provider_used."
        ),
    )
    stream: bool = Field(
        default=False,
        description=(
            "Stream the response through the provider's streaming API and forward each text "
            "delta as a node.token event to live watchers (WebSocket). The node output is the same."
        ),
    )

    @field_validator("fallback")
    @classmethod
    def _known_providers(cls, entries: list[str]) -> list[str]:
        for entry in entries:
            provider, _ = parse_chain_entry(entry)
            if provider not in LLM_PROVIDER_NAMES:
                raise ValueError(f"unknown provider '{provider}' (known: {', '.join(LLM_PROVIDER_NAMES)})")
        return entries


class GeminiConfig(LLMConfig):
    provider: LLMProviderName = "gemini"
    # Gemini 2.5+ thinks by default and thinking counts toward this limit.
    max_tokens: int = Field(default=8192, ge=1, le=65536)


class GroqConfig(LLMConfig):
    provider: LLMProviderName = "groq"


class OpenRouterConfig(LLMConfig):
    provider: LLMProviderName = "openrouter"


class OllamaConfig(LLMConfig):
    provider: LLMProviderName = "ollama"


class OpenAIConfig(LLMConfig):
    provider: LLMProviderName = "openai"


class AnthropicConfig(LLMConfig):
    provider: LLMProviderName = "anthropic"
    temperature: float = Field(
        default=1.0,
        ge=0,
        le=1,
        description="Ignored by current Claude models, which don't accept sampling parameters.",
    )
    # Covers adaptive thinking + the answer; above 16k the adapter streams internally.
    max_tokens: int = Field(default=16000, ge=1, le=64000)


class LLMResult(BaseModel):
    response: str
    provider: str
    provider_used: str
    model: str
    mock: bool
    fallback_errors: list[dict[str, Any]]


class LLMNode(NodeDefinition[LLMConfig]):
    category = "ai"
    output_schema = LLMResult

    def _default_provider(self) -> str:
        return str(self.config_schema.model_fields["provider"].default)

    def required_providers(self, node: GraphNode) -> list[tuple[str, str]]:
        # Templated values can only be checked at run time; unknown names are already
        # reported as invalid config.
        candidates = [(node.config.get("provider", self._default_provider()), "provider")]
        fallback = node.config.get("fallback")
        if isinstance(fallback, list):
            candidates += [(entry, "fallback") for entry in fallback]
        required = []
        for value, field in candidates:
            if isinstance(value, str) and not contains_reference(value):
                name = parse_chain_entry(value)[0] if field == "fallback" else value.lower()
                if name in LLM_PROVIDER_NAMES:
                    required.append((name, field))
        return required

    async def execute(self, context: NodeContext, config: LLMConfig) -> NodeResult:
        chain = [(config.provider, config.model), *(parse_chain_entry(e) for e in config.fallback)]
        errors: list[dict[str, Any]] = []

        for provider_name, requested_model in chain:
            model = requested_model or context.services.default_model(provider_name)
            try:
                provider = context.services.llm(provider_name)
                request = {
                    "system_prompt": config.system_prompt,
                    "user_prompt": config.user_prompt,
                    "model": model,
                    "temperature": config.temperature,
                    "max_tokens": config.max_tokens,
                }
                if config.stream and context.on_token is not None:
                    text = await _collect_stream(provider, provider_name, request, context.on_token)
                else:
                    text = await provider.generate(**request)
            except ProviderError as exc:
                errors.append({"provider": provider_name, "model": model, "error": str(exc)})
                if len(chain) > 1:
                    logger.warning(
                        "LLM provider failed; trying the next one in the chain",
                        extra={"node_id": context.node_id, "provider": provider_name, "error": str(exc)},
                    )
                continue

            logger.info(
                "LLM node answered",
                extra={
                    "node_id": context.node_id,
                    "provider_used": provider_name,
                    "model": model,
                    "mock": provider.is_mock,
                    "failed_providers": [e["provider"] for e in errors],
                },
            )
            return NodeResult.ok(
                response=text,
                provider=config.provider,
                provider_used=provider_name,
                model=model,
                mock=provider.is_mock,
                fallback_errors=errors,
            )

        if len(errors) == 1:
            return NodeResult.fail(errors[0]["error"], fallback_errors=errors)
        summary = "; ".join(e["error"] for e in errors)
        return NodeResult.fail(f"All {len(errors)} providers failed: {summary}", fallback_errors=errors)


async def _collect_stream(
    provider: Any, provider_name: str, request: dict[str, Any], on_token: Any
) -> str:
    """Stream the answer, forwarding each delta; returns the full text.

    If the provider fails mid-stream and the fallback chain continues, later tokens carry
    the next provider's name, so watchers can tell the attempts apart.
    """
    parts: list[str] = []
    async for chunk in provider.stream(**request):
        parts.append(chunk)
        await on_token(chunk, provider_name)
    text = "".join(parts)
    if not text:
        raise ProviderError(provider_name, "stream ended without any text")
    return text


@register_node("gemini")
class GeminiNode(LLMNode):
    label = "Gemini"
    description = "Generates text with Google Gemini (free tier via Google AI Studio)."
    icon = "sparkles"
    config_schema = GeminiConfig


@register_node("groq")
class GroqNode(LLMNode):
    label = "Groq"
    description = "Generates text with open models (Llama, GPT-OSS) on Groq."
    icon = "zap"
    config_schema = GroqConfig


@register_node("openrouter")
class OpenRouterNode(LLMNode):
    label = "OpenRouter"
    description = "Generates text with any OpenRouter model, including ':free' ones."
    icon = "route"
    config_schema = OpenRouterConfig


@register_node("ollama")
class OllamaNode(LLMNode):
    label = "Ollama"
    description = "Generates text with a local Ollama model (no API key)."
    icon = "cpu"
    config_schema = OllamaConfig


@register_node("openai")
class OpenAINode(LLMNode):
    label = "OpenAI"
    description = "Generates text with an OpenAI chat model."
    icon = "bot"
    config_schema = OpenAIConfig


@register_node("anthropic")
class AnthropicNode(LLMNode):
    label = "Claude"
    description = "Generates text with Anthropic Claude."
    icon = "brain"
    config_schema = AnthropicConfig
