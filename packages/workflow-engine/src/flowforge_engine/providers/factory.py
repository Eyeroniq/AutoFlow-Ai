"""Builds providers from ProviderSettings.

Policy: a real provider when its credentials are configured, otherwise
MissingCredentialsError ("Authentication missing ..."). Mocks are handed out only when
settings.testing is set (the pytest suite, TESTING=true) or the caller asks for the
provider named "mock".
"""

from __future__ import annotations

import logging
from typing import Any

from flowforge_engine.errors import MissingCredentialsError
from flowforge_engine.providers.base import EmailProvider, LLMProvider, MailboxProvider
from flowforge_engine.providers.mock import MockDiscordProvider, MockEmailProvider, MockLLMProvider, MockTelegramProvider
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
    "get_discord_provider",
    "get_email_provider",
    "get_llm_provider",
    "get_mailbox_provider",
    "get_search_provider",
    "get_workspace_client",
    "get_telegram_provider",
    "get_transcriber",
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
        allow_private_network=settings.allow_private_network,
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


def get_telegram_provider(provider_name: str = "telegram", settings: ProviderSettings | None = None) -> Any:
    """TelegramProvider with TELEGRAM_BOT_TOKEN or the user's stored bot token."""
    settings = settings if settings is not None else ProviderSettings.from_env()
    name = provider_name.lower()
    if name not in ("telegram", "mock"):
        raise ValueError(f"Unknown Telegram provider '{provider_name}' (known: telegram, mock)")
    if name == "mock" or settings.testing:
        return MockTelegramProvider(name)
    if not settings.has_credentials("telegram"):
        raise MissingCredentialsError("telegram", missing_credentials_hint("telegram"))
    from flowforge_engine.providers.telegram_provider import TelegramProvider

    token = settings.telegram.bot_token
    assert token is not None
    return TelegramProvider(token.get_secret_value(), name=name, retry=settings.retry)


def get_discord_provider(
    provider_name: str = "discord", settings: ProviderSettings | None = None, *, webhook_url: str | None = None
) -> Any:
    """DiscordWebhookProvider for `webhook_url`, or the configured one (DISCORD_WEBHOOK_URL or
    the user's stored webhook). Raises ValueError for a URL that isn't a Discord webhook."""
    settings = settings if settings is not None else ProviderSettings.from_env()
    name = provider_name.lower()
    if name not in ("discord", "mock"):
        raise ValueError(f"Unknown Discord provider '{provider_name}' (known: discord, mock)")
    if name == "mock" or settings.testing:
        if webhook_url:
            from flowforge_engine.providers.discord_provider import parse_webhook_url

            parse_webhook_url(webhook_url)  # same URL check as for real
        return MockDiscordProvider(name)
    if not webhook_url:
        if not settings.has_credentials("discord"):
            raise MissingCredentialsError("discord", missing_credentials_hint("discord"))
        assert settings.discord.webhook_url is not None
        webhook_url = settings.discord.webhook_url.get_secret_value()
    from flowforge_engine.providers.discord_provider import DiscordWebhookProvider

    return DiscordWebhookProvider(webhook_url, name=name, retry=settings.retry)


def get_transcriber(provider_name: str = "groq", settings: ProviderSettings | None = None) -> Any:
    """Speech-to-text: "groq" (Whisper API, the groq account's key) or "local" (faster-whisper)."""
    settings = settings if settings is not None else ProviderSettings.from_env()
    name = provider_name.lower()
    if name not in ("groq", "local", "mock"):
        raise ValueError(f"Unknown speech-to-text provider '{provider_name}' (known: groq, local)")
    from flowforge_engine.providers.transcription import GroqTranscriber, LocalWhisperTranscriber, MockTranscriber

    if name == "mock" or settings.testing:
        return MockTranscriber(name)
    speech = settings.speech
    if name == "local":
        return LocalWhisperTranscriber(model=speech.local_model, models_dir=speech.models_dir, compute_type=speech.local_compute_type)
    if not settings.has_credentials("groq"):
        raise MissingCredentialsError("groq", missing_credentials_hint("groq"))
    key = settings.groq.api_key
    assert key is not None
    return GroqTranscriber(key.get_secret_value(), retry=settings.retry, max_file_mb=speech.groq_max_file_mb)


def get_workspace_client(provider_name: str, settings: ProviderSettings | None = None) -> Any:
    """Notion ("notion", NOTION_API_KEY or the user's token) or Airtable ("airtable",
    AIRTABLE_API_KEY or the user's token). Raises MissingCredentialsError without a token."""
    settings = settings if settings is not None else ProviderSettings.from_env()
    name = provider_name.lower()
    if name not in ("notion", "airtable"):
        raise ValueError(f"Unknown workspace app '{provider_name}' (known: notion, airtable)")
    from flowforge_engine.providers.workspace import AirtableClient, MockAirtable, MockNotion, NotionClient

    if settings.testing:
        return MockNotion() if name == "notion" else MockAirtable()
    if not settings.has_credentials(name):
        raise MissingCredentialsError(name, missing_credentials_hint(name))
    token = getattr(settings, name).api_key
    assert token is not None
    cls = NotionClient if name == "notion" else AirtableClient
    return cls(token.get_secret_value(), retry=settings.retry)


def get_search_provider(provider_name: str = "duckduckgo", settings: ProviderSettings | None = None) -> Any:
    """Web search: "duckduckgo" (no key) or "tavily" (TAVILY_API_KEY or the user's key)."""
    settings = settings if settings is not None else ProviderSettings.from_env()
    name = provider_name.lower()
    if name not in ("duckduckgo", "tavily", "mock"):
        raise ValueError(f"Unknown search provider '{provider_name}' (known: duckduckgo, tavily)")
    from flowforge_engine.providers.search import DuckDuckGoSearch, MockSearch, TavilySearch

    if name == "mock" or settings.testing:
        return MockSearch(name)
    if name == "duckduckgo":
        return DuckDuckGoSearch()
    if not settings.has_credentials("tavily"):
        raise MissingCredentialsError("tavily", missing_credentials_hint("tavily"))
    key = settings.tavily.api_key
    assert key is not None
    return TavilySearch(key.get_secret_value(), retry=settings.retry)
