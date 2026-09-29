from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import httpx

from flowforge_engine.files import FileNotAvailable, FileStore
from flowforge_engine.netguard import ALLOW_ENV
from flowforge_engine.providers.base import EmailProvider, LLMProvider, MailboxProvider
from flowforge_engine.providers.factory import (
    get_discord_provider,
    get_email_provider,
    get_llm_provider,
    get_mailbox_provider,
    get_search_provider,
    get_telegram_provider,
    get_transcriber,
)
from flowforge_engine.providers.settings import ProviderSettings, missing_credentials_hint
from flowforge_engine.state import MemoryStateStore, NodeStateStore


class ExecutionServices:
    """Provider access for nodes, resolved lazily and cached per instance.

    `provider_settings` carries the credentials (the API builds one per run: server .env
    defaults overlaid with the user's own keys); it defaults to the environment. Pass
    explicit providers to override the factory (tests). `http_transport` lets tests
    intercept outbound HTTP (HTTP Request, RSS, Web Page). `state` keeps node state between
    runs (RSS "since last run"); the API passes a database-backed store.
    """

    def __init__(
        self,
        *,
        provider_settings: ProviderSettings | None = None,
        llm_providers: Mapping[str, LLMProvider] | None = None,
        email_providers: Mapping[str, EmailProvider] | None = None,
        mailbox_providers: Mapping[str, MailboxProvider] | None = None,
        messaging_providers: Mapping[str, Any] | None = None,
        transcribers: Mapping[str, Any] | None = None,
        search_providers: Mapping[str, Any] | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        files: FileStore | None = None,
        allow_private_network: bool | None = None,
        state: NodeStateStore | None = None,
    ):
        self.settings = provider_settings if provider_settings is not None else ProviderSettings.from_env()
        self._llm: dict[str, LLMProvider] = dict(llm_providers or {})
        self._email: dict[str, EmailProvider] = dict(email_providers or {})
        self._mailbox: dict[str, MailboxProvider] = dict(mailbox_providers or {})
        # Telegram ("telegram") and Discord ("discord") senders; injected ones win (tests).
        self._messaging: dict[str, Any] = dict(messaging_providers or {})
        # Speech-to-text ("groq", "local") and web search ("duckduckgo", "tavily").
        self._transcribers: dict[str, Any] = dict(transcribers or {})
        self._search: dict[str, Any] = dict(search_providers or {})
        self._injected = (
            set(self._llm) | set(self._email) | set(self._mailbox) | set(self._messaging)
            | set(self._transcribers) | set(self._search)
        )
        self.state: NodeStateStore = state if state is not None else MemoryStateStore()
        self.http_transport = http_transport
        self._files = files
        # The HTTP Request node's SSRF guard (flowforge_engine.netguard) is on unless this is
        # True; None reads HTTP_ALLOW_PRIVATE_NETWORKS from the environment.
        if allow_private_network is None:
            allow_private_network = os.environ.get(ALLOW_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
        self.allow_private_network = allow_private_network

    @property
    def files(self) -> FileStore:
        """The run owner's uploaded files. Raises FileNotAvailable when none are configured."""
        if self._files is None:
            raise FileNotAvailable("File storage isn't available here (no file store was configured for this run)")
        return self._files

    def llm(self, provider_name: str) -> LLMProvider:
        """Raises MissingCredentialsError if the provider has no credentials."""
        if provider_name not in self._llm:
            self._llm[provider_name] = get_llm_provider(provider_name, self.settings)
        return self._llm[provider_name]

    def email(self, provider_name: str) -> EmailProvider:
        if provider_name not in self._email:
            self._email[provider_name] = get_email_provider(provider_name, self.settings)
        return self._email[provider_name]

    def mailbox(self, provider_name: str) -> MailboxProvider:
        if provider_name not in self._mailbox:
            self._mailbox[provider_name] = get_mailbox_provider(provider_name, self.settings)
        return self._mailbox[provider_name]

    def telegram(self, provider_name: str = "telegram") -> Any:
        """Raises MissingCredentialsError without a bot token."""
        if provider_name not in self._messaging:
            self._messaging[provider_name] = get_telegram_provider(provider_name, self.settings)
        return self._messaging[provider_name]

    def telegram_default_chat(self) -> str | None:
        """The chat a Telegram node without a chat_id sends to (the credential's chat id)."""
        return self.settings.telegram.chat_id

    def discord(self, provider_name: str = "discord", webhook_url: str | None = None) -> Any:
        """The configured webhook, or `webhook_url` (a node's own). Raises ValueError for a
        URL that isn't a Discord webhook, MissingCredentialsError when none is configured."""
        if provider_name in self._injected and provider_name in self._messaging:  # tests
            return self._messaging[provider_name]
        if webhook_url is not None:
            return get_discord_provider(provider_name, self.settings, webhook_url=webhook_url)
        if provider_name not in self._messaging:
            self._messaging[provider_name] = get_discord_provider(provider_name, self.settings)
        return self._messaging[provider_name]

    def transcriber(self, provider_name: str = "groq") -> Any:
        """Raises MissingCredentialsError for groq without a key."""
        if provider_name not in self._transcribers:
            self._transcribers[provider_name] = get_transcriber(provider_name, self.settings)
        return self._transcribers[provider_name]

    def search(self, provider_name: str = "duckduckgo") -> Any:
        """Raises MissingCredentialsError for tavily without a key."""
        if provider_name not in self._search:
            self._search[provider_name] = get_search_provider(provider_name, self.settings)
        return self._search[provider_name]

    def has_credentials(self, provider_name: str) -> bool:
        return provider_name in self._injected or self.settings.has_credentials(provider_name)

    def missing_credentials_message(self, provider_name: str) -> str:
        return f"Authentication missing for provider '{provider_name}': {missing_credentials_hint(provider_name)}"

    def default_model(self, provider_name: str) -> str:
        return self.settings.default_model(provider_name)
