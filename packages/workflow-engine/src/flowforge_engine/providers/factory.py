from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, SecretStr, field_validator

from flowforge_engine.providers.base import EmailProvider, LLMProvider
from flowforge_engine.providers.mock import MockEmailProvider, MockLLMProvider

logger = logging.getLogger(__name__)


class ProviderSettings(BaseModel):
    """API keys for real providers. A missing or blank key means "use the mock"."""

    gemini_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @classmethod
    def from_env(cls) -> ProviderSettings:
        return cls(
            gemini_api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"),
            openai_api_key=os.environ.get("OPENAI_API_KEY"),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        )


def _gemini(api_key: str) -> LLMProvider:
    from flowforge_engine.providers.gemini_provider import GeminiProvider

    return GeminiProvider(api_key)


def _openai(api_key: str) -> LLMProvider:
    from flowforge_engine.providers.openai_provider import OpenAIProvider

    return OpenAIProvider(api_key)


def _anthropic(api_key: str) -> LLMProvider:
    from flowforge_engine.providers.anthropic_provider import AnthropicProvider

    return AnthropicProvider(api_key)


_LLM_FACTORIES: dict[str, Callable[[str], LLMProvider]] = {
    "gemini": _gemini,
    "openai": _openai,
    "anthropic": _anthropic,
}

LLM_PROVIDER_NAMES = tuple(_LLM_FACTORIES)
EMAIL_PROVIDER_NAMES = ("gmail",)


def get_llm_provider(provider_name: str, settings: ProviderSettings | None = None) -> LLMProvider:
    """Real adapter when its API key is configured, otherwise MockLLMProvider.

    `settings` defaults to reading GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY
    from the environment.
    """
    name = provider_name.lower()
    factory = _LLM_FACTORIES.get(name)
    if factory is None:
        raise ValueError(f"Unknown LLM provider '{provider_name}' (known: {', '.join(LLM_PROVIDER_NAMES)})")

    settings = settings if settings is not None else ProviderSettings.from_env()
    key: SecretStr | None = getattr(settings, f"{name}_api_key")
    if key is None:
        logger.warning(
            "no API key configured for %s; using MockLLMProvider (mock mode)",
            name,
            extra={"provider": name, "mock": True},
        )
        return MockLLMProvider(name)
    return factory(key.get_secret_value())


def get_email_provider(provider_name: str, settings: ProviderSettings | None = None) -> EmailProvider:
    """Always the mock in this phase — real Gmail needs OAuth (integrations phase)."""
    name = provider_name.lower()
    if name not in EMAIL_PROVIDER_NAMES:
        raise ValueError(f"Unknown email provider '{provider_name}' (known: {', '.join(EMAIL_PROVIDER_NAMES)})")
    logger.info("using MockEmailProvider for %s", name, extra={"provider": name, "mock": True})
    return MockEmailProvider(name)
