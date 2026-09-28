"""Wires the engine's providers to the API's Settings (i.e. the .env API keys)."""

from functools import lru_cache

from flowforge_engine import ExecutionServices
from flowforge_engine.providers import LLMProvider, ProviderSettings
from flowforge_engine.providers import get_llm_provider as _engine_get_llm_provider

from app.core.config import settings


def provider_settings() -> ProviderSettings:
    return ProviderSettings(
        gemini_api_key=settings.GEMINI_API_KEY,
        openai_api_key=settings.OPENAI_API_KEY,
        anthropic_api_key=settings.ANTHROPIC_API_KEY,
    )


def get_llm_provider(provider_name: str) -> LLMProvider:
    """The real adapter if its key is set in .env, otherwise MockLLMProvider (logs a warning)."""
    return _engine_get_llm_provider(provider_name, provider_settings())


@lru_cache
def get_execution_services() -> ExecutionServices:
    """Process-wide services, so real SDK clients (and their connection pools) are reused."""
    return ExecutionServices(provider_settings=provider_settings())
