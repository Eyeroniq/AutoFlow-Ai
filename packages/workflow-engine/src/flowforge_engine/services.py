from __future__ import annotations

from collections.abc import Mapping

import httpx

from flowforge_engine.providers.base import EmailProvider, LLMProvider
from flowforge_engine.providers.factory import ProviderSettings, get_email_provider, get_llm_provider


class ExecutionServices:
    """Provider access for nodes, resolved lazily and cached per instance.

    Pass explicit providers to override the factory (tests), or `provider_settings` to
    choose real vs mock providers. `http_transport` lets tests intercept HTTPRequestNode.
    """

    def __init__(
        self,
        *,
        provider_settings: ProviderSettings | None = None,
        llm_providers: Mapping[str, LLMProvider] | None = None,
        email_providers: Mapping[str, EmailProvider] | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._settings = provider_settings
        self._llm: dict[str, LLMProvider] = dict(llm_providers or {})
        self._email: dict[str, EmailProvider] = dict(email_providers or {})
        self.http_transport = http_transport

    def llm(self, provider_name: str) -> LLMProvider:
        if provider_name not in self._llm:
            self._llm[provider_name] = get_llm_provider(provider_name, self._settings)
        return self._llm[provider_name]

    def email(self, provider_name: str) -> EmailProvider:
        if provider_name not in self._email:
            self._email[provider_name] = get_email_provider(provider_name, self._settings)
        return self._email[provider_name]
