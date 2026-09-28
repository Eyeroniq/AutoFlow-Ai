"""LLM and email provider abstractions.

Real adapters (GeminiProvider, OpenAIProvider, AnthropicProvider) live in their own
modules and import their SDKs lazily, so the engine works without them installed.
"""

from flowforge_engine.providers.base import EmailProvider, LLMProvider
from flowforge_engine.providers.factory import (
    EMAIL_PROVIDER_NAMES,
    LLM_PROVIDER_NAMES,
    ProviderSettings,
    get_email_provider,
    get_llm_provider,
)
from flowforge_engine.providers.mock import MockEmailProvider, MockLLMProvider, SentEmail

__all__ = [
    "EMAIL_PROVIDER_NAMES",
    "LLM_PROVIDER_NAMES",
    "EmailProvider",
    "LLMProvider",
    "MockEmailProvider",
    "MockLLMProvider",
    "ProviderSettings",
    "SentEmail",
    "get_email_provider",
    "get_llm_provider",
]
