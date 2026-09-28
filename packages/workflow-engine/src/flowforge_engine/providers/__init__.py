"""LLM and email providers.

Real adapters live in their own modules and import their SDKs lazily, so the engine
works without them installed:

- GeminiProvider (google-genai), AnthropicProvider (anthropic)
- OpenAICompatibleProvider (openai SDK) for Groq, OpenRouter, Ollama, and OpenAI
- SMTPEmailProvider / IMAPEmailProvider (stdlib) for Gmail or any SMTP/IMAP mailbox
"""

from flowforge_engine.providers.base import (
    EmailAttachment,
    EmailProvider,
    LLMProvider,
    MailboxProvider,
    MailboxQuery,
    OutgoingEmail,
)
from flowforge_engine.providers.factory import get_email_provider, get_llm_provider, get_mailbox_provider
from flowforge_engine.providers.mock import MockEmailProvider, MockLLMProvider, SentEmail
from flowforge_engine.providers.retry import RetryPolicy, with_retries
from flowforge_engine.providers.settings import (
    EMAIL_PROVIDER_NAMES,
    EMAIL_PROVIDERS,
    LLM_PROVIDER_NAMES,
    LLM_PROVIDERS,
    EmailAccount,
    LLMAccount,
    ProviderInfo,
    ProviderSettings,
)

__all__ = [
    "EMAIL_PROVIDERS",
    "EMAIL_PROVIDER_NAMES",
    "LLM_PROVIDERS",
    "LLM_PROVIDER_NAMES",
    "EmailAccount",
    "EmailAttachment",
    "EmailProvider",
    "LLMAccount",
    "LLMProvider",
    "MailboxProvider",
    "MailboxQuery",
    "MockEmailProvider",
    "MockLLMProvider",
    "OutgoingEmail",
    "ProviderInfo",
    "ProviderSettings",
    "RetryPolicy",
    "SentEmail",
    "get_email_provider",
    "get_llm_provider",
    "get_mailbox_provider",
    "with_retries",
]
