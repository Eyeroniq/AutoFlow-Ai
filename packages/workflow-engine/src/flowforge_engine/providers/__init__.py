"""LLM and email providers.

Real adapters live in their own modules and import their SDKs lazily, so the engine
works without them installed:

- GeminiProvider (google-genai), AnthropicProvider (anthropic)
- OpenAICompatibleProvider (openai SDK) for Groq, OpenRouter, Ollama, and OpenAI
- SMTPEmailProvider / IMAPEmailProvider (stdlib) for Gmail or any SMTP/IMAP mailbox
- TelegramProvider (Bot API) and DiscordWebhookProvider (channel webhooks), over httpx
- GroqTranscriber (Whisper API) and LocalWhisperTranscriber (faster-whisper) for speech
- DuckDuckGoSearch (ddgs) and TavilySearch for web search
"""

from flowforge_engine.providers.base import (
    EmailAttachment,
    EmailProvider,
    LLMProvider,
    MailboxProvider,
    MailboxQuery,
    OutgoingEmail,
)
from flowforge_engine.providers.factory import (
    get_discord_provider,
    get_email_provider,
    get_llm_provider,
    get_mailbox_provider,
    get_search_provider,
    get_workspace_client,
    get_telegram_provider,
    get_transcriber,
)
from flowforge_engine.providers.mock import (
    MockDiscordProvider,
    MockEmailProvider,
    MockLLMProvider,
    MockTelegramProvider,
    SentEmail,
    SentMessage,
)
from flowforge_engine.providers.retry import RetryPolicy, with_retries
from flowforge_engine.providers.settings import (
    EMAIL_PROVIDER_NAMES,
    EMAIL_PROVIDERS,
    LLM_PROVIDER_NAMES,
    LLM_PROVIDERS,
    MESSAGING_PROVIDERS,
    SEARCH_PROVIDERS,
    WORKSPACE_PROVIDERS,
    DiscordAccount,
    EmailAccount,
    LLMAccount,
    ProviderInfo,
    ProviderSettings,
    SearchAccount,
    SpeechSettings,
    TelegramAccount,
)

__all__ = [
    "EMAIL_PROVIDERS",
    "EMAIL_PROVIDER_NAMES",
    "LLM_PROVIDERS",
    "LLM_PROVIDER_NAMES",
    "DiscordAccount",
    "EmailAccount",
    "EmailAttachment",
    "EmailProvider",
    "LLMAccount",
    "LLMProvider",
    "MESSAGING_PROVIDERS",
    "SEARCH_PROVIDERS",
    "WORKSPACE_PROVIDERS",
    "MailboxProvider",
    "MailboxQuery",
    "MockDiscordProvider",
    "MockEmailProvider",
    "MockLLMProvider",
    "MockTelegramProvider",
    "OutgoingEmail",
    "ProviderInfo",
    "ProviderSettings",
    "RetryPolicy",
    "SentEmail",
    "SearchAccount",
    "SentMessage",
    "SpeechSettings",
    "TelegramAccount",
    "get_discord_provider",
    "get_email_provider",
    "get_llm_provider",
    "get_mailbox_provider",
    "get_search_provider",
    "get_workspace_client",
    "get_telegram_provider",
    "get_transcriber",
    "with_retries",
]
