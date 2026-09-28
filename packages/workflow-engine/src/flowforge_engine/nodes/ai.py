from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from flowforge_engine.errors import ProviderError
from flowforge_engine.models import NodeContext, NodeResult
from flowforge_engine.providers import anthropic_provider, gemini_provider, openai_provider
from flowforge_engine.registry import NodeConfig, NodeDefinition, register_node


class LLMConfig(NodeConfig):
    model: str = Field(min_length=1)
    system_prompt: str = ""
    user_prompt: str = Field(min_length=1)
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(ge=1)


class GeminiConfig(LLMConfig):
    model: str = gemini_provider.DEFAULT_MODEL
    # Gemini 2.5+ thinks by default and thinking counts toward this limit.
    max_tokens: int = Field(default=8192, ge=1, le=65536)


class OpenAIConfig(LLMConfig):
    model: str = openai_provider.DEFAULT_MODEL
    max_tokens: int = Field(default=4096, ge=1, le=128000)


class AnthropicConfig(LLMConfig):
    model: str = anthropic_provider.DEFAULT_MODEL
    temperature: float = Field(
        default=1.0,
        ge=0,
        le=1,
        description="Ignored by current Claude models, which don't accept sampling parameters.",
    )
    # Covers adaptive thinking + the answer. Kept under ~21k: larger non-streaming
    # requests are refused by the SDK because they risk HTTP timeouts.
    max_tokens: int = Field(default=16000, ge=1, le=20000)


class LLMResult(BaseModel):
    response: str
    provider: str
    model: str
    mock: bool


class LLMNode(NodeDefinition[LLMConfig]):
    provider: ClassVar[str]
    category = "ai"
    output_schema = LLMResult

    async def execute(self, context: NodeContext, config: LLMConfig) -> NodeResult:
        provider = context.services.llm(self.provider)
        try:
            text = await provider.generate(
                system_prompt=config.system_prompt,
                user_prompt=config.user_prompt,
                model=config.model,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
            )
        except ProviderError as exc:
            return NodeResult.fail(str(exc))
        return NodeResult.ok(response=text, provider=self.provider, model=config.model, mock=provider.is_mock)


@register_node("gemini")
class GeminiNode(LLMNode):
    provider = "gemini"
    label = "Gemini"
    description = "Generates text with Google Gemini."
    icon = "sparkles"
    config_schema = GeminiConfig


@register_node("openai")
class OpenAINode(LLMNode):
    provider = "openai"
    label = "OpenAI"
    description = "Generates text with an OpenAI chat model."
    icon = "bot"
    config_schema = OpenAIConfig


@register_node("anthropic")
class AnthropicNode(LLMNode):
    provider = "anthropic"
    label = "Claude"
    description = "Generates text with Anthropic Claude."
    icon = "brain"
    config_schema = AnthropicConfig
