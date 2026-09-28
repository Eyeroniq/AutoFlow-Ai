"""Builds providers from ProviderSettings.

Policy: a real provider when its credentials are configured, otherwise
MissingCredentialsError ("Authentication missing ..."). Mocks are handed out only when
settings.testing is set (the pytest suite, TESTING=true) or the caller asks for the
provider named "mock".
"""

from __future__ import annotations

import logging

from flowforge_engine.errors import MissingCredentialsError
from flowforge_engine.providers.base import EmailProvider, LLMProvider, MailboxProvider
from flowforge_engine.providers.mock import MockEmailProvider, MockLLMProvider
from flowforge_engine.providers.settings import (
    EMAIL_PROVIDER_NAMES,
    EMAIL_PROVIDERS,
    LLM_PROVIDER_NAMES,
    LLM_PROVIDERS,
    EmailAccount,
    ProviderSettings,
    missing_credentials_hint,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EMAIL_PROVIDER_NAMES",
    "LLM_PROVIDER_NAMES",
    "ProviderSettings",
    "get_email_provider",
    "get_llm_provider",
    "get_mailbox_provider",
]


def _llm_name(provider_name: str) -> str:
    name = provider_name.lower()
    if name not in LLM_PROVIDERS:
        raise ValueError(f"Unknown LLM provider '{provider_name}' (known: {', '.join(LLM_PROVIDER_NAMES)})")
    return name


def get_llm_provider(provider_name: str, settings: ProviderSettings | None = None) -> LLMProvider:
    """The real adapter for `provider_name`.

    Raises MissingCredentialsError when it needs an API key and none is configured.
    `settings` defaults to reading the environment (GEMINI_API_KEY, GROQ_API_KEY, ...).
    """
    name = _llm_name(provider_name)
    settings = settings if settings is not None else ProviderSettings.from_env()
    if name == "mock":
        return MockLLMProvider("mock")
    if settings.testing:
        logger.debug("TESTING is set; using MockLLMProvider", extra={"provider": name, "mock": True})
        return MockLLMProvider(name)
    if not settings.has_credentials(name):
        raise MissingCredentialsError(name, missing_credentials_hint(name))

    account = settings.llm_account(name)
    api_key = account.api_key.get_secret_value() if account.api_key else None
    common = {"retry": settings.retry, "timeout": settings.request_timeout_seconds}

    if name == "gemini":
        from flowforge_engine.providers.gemini_provider import GeminiProvider

        return GeminiProvider(api_key or "", embedding_model=settings.embedding_model(name) or "", **common)
    if name == "anthropic":
        from flowforge_engine.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key or "", **common)

    from flowforge_engine.providers.openai_compatible import OpenAICompatibleProvider

    return OpenAICompatibleProvider(
        name,
        api_key=api_key,
        base_url=settings.base_url(name),
        embedding_model=settings.embedding_model(name),
        **common,
    )


def _email_account(provider_name: str, settings: ProviderSettings) -> tuple[str, EmailAccount | None]:
    name = provider_name.lower()
    if name not in EMAIL_PROVIDERS:
        raise ValueError(f"Unknown email provider '{provider_name}' (known: {', '.join(EMAIL_PROVIDER_NAMES)})")
    if name == "mock" or settings.testing:
        return name, None
    if not settings.has_credentials(name):
        raise MissingCredentialsError(name, missing_credentials_hint(name))
    return name, settings.email_account(name)


def get_email_provider(provider_name: str, settings: ProviderSettings | None = None) -> EmailProvider:
    """SMTPEmailProvider for "gmail" (SMTP_USER/SMTP_PASSWORD or a stored credential)."""
    settings = settings if settings is not None else ProviderSettings.from_env()
    name, account = _email_account(provider_name, settings)
    if account is None:
        return MockEmailProvider(name)
    from flowforge_engine.providers.smtp_provider import SMTPEmailProvider

    return SMTPEmailProvider(account, name=name, retry=settings.retry)


def get_mailbox_provider(provider_name: str, settings: ProviderSettings | None = None) -> MailboxProvider:
    """IMAPEmailProvider for "gmail" — same account as sending."""
    settings = settings if settings is not None else ProviderSettings.from_env()
    name, account = _email_account(provider_name, settings)
    if account is None:
        return MockEmailProvider(name)
    from flowforge_engine.providers.imap_provider import IMAPEmailProvider

    return IMAPEmailProvider(account, name=name, retry=settings.retry)
