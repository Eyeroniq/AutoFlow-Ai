from __future__ import annotations

import os
from collections.abc import Mapping

import httpx

from flowforge_engine.files import FileNotAvailable, FileStore
from flowforge_engine.netguard import ALLOW_ENV
from flowforge_engine.providers.base import EmailProvider, LLMProvider, MailboxProvider
from flowforge_engine.providers.factory import get_email_provider, get_llm_provider, get_mailbox_provider
from flowforge_engine.providers.settings import ProviderSettings, missing_credentials_hint


class ExecutionServices:
    """Provider access for nodes, resolved lazily and cached per instance.

    `provider_settings` carries the credentials (the API builds one per run: server .env
    defaults overlaid with the user's own keys); it defaults to the environment. Pass
    explicit providers to override the factory (tests). `http_transport` lets tests
    intercept HTTPRequestNode.
    """

    def __init__(
        self,
        *,
        provider_settings: ProviderSettings | None = None,
        llm_providers: Mapping[str, LLMProvider] | None = None,
        email_providers: Mapping[str, EmailProvider] | None = None,
        mailbox_providers: Mapping[str, MailboxProvider] | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        files: FileStore | None = None,
        allow_private_network: bool | None = None,
    ):
        self.settings = provider_settings if provider_settings is not None else ProviderSettings.from_env()
        self._llm: dict[str, LLMProvider] = dict(llm_providers or {})
        self._email: dict[str, EmailProvider] = dict(email_providers or {})
        self._mailbox: dict[str, MailboxProvider] = dict(mailbox_providers or {})
        self._injected = set(self._llm) | set(self._email) | set(self._mailbox)
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

    def has_credentials(self, provider_name: str) -> bool:
        return provider_name in self._injected or self.settings.has_credentials(provider_name)

    def missing_credentials_message(self, provider_name: str) -> str:
        return f"Authentication missing for provider '{provider_name}': {missing_credentials_hint(provider_name)}"

    def default_model(self, provider_name: str) -> str:
        return self.settings.default_model(provider_name)
