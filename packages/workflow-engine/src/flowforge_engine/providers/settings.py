"""Provider catalog and credentials.

`ProviderSettings` holds everything the factory needs to build real providers: API keys,
base URLs, default models, SMTP/IMAP accounts, and the retry policy. The API builds one
per run from the server .env (server-wide defaults) overlaid with the user's own stored
credentials, which take priority.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

from flowforge_engine.providers.retry import RetryPolicy

ProviderKind = Literal["llm", "email", "messaging", "search", "workspace"]


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    label: str
    kind: ProviderKind
    # Where the adapter talks to (None: the SDK's own default endpoint).
    base_url: str | None = None
    default_model: str | None = None
    embedding_model: str | None = None
    requires_key: bool = True
    # Server-wide environment variable(s) that configure it.
    env_vars: tuple[str, ...] = ()
    # Where a user gets a (free) key.
    get_key_url: str | None = None


# Default models were checked against each provider's docs/model list (2026-09) and chosen
# for free-tier headroom. Every one can be overridden with the matching *_MODEL
# environment variable or per node. (Gemini: gemini-3.8-flash is stronger, but its free
# tier allowed only 20 requests/day per project when checked; flash-lite allows far more.)
LLM_PROVIDERS: dict[str, ProviderInfo] = {
    info.name: info
    for info in (
        ProviderInfo(
            "gemini", "Google Gemini", "llm",
            default_model="gemini-3.5-flash-lite", embedding_model="gemini-embedding-2",
            env_vars=("GEMINI_API_KEY",), get_key_url="https://aistudio.google.com/apikey",
        ),
        ProviderInfo(
            "groq", "Groq", "llm", base_url="https://api.groq.com/openai/v1",
            # A Groq production model. (Model access varies by account: the Llama models
            # weren't offered to the key this was verified with; /test says what is.)
            default_model="openai/gpt-oss-20b",
            env_vars=("GROQ_API_KEY",), get_key_url="https://console.groq.com/keys",
        ),
        ProviderInfo(
            "openrouter", "OpenRouter", "llm", base_url="https://openrouter.ai/api/v1",
            # A router over whichever ":free" models are currently available.
            default_model="openrouter/free",
            env_vars=("OPENROUTER_API_KEY",), get_key_url="https://openrouter.ai/settings/keys",
        ),
        # Mistral La Plateforme, free "Experiment" plan. Limits are per model: a free account was
        # given 0 requests/min on mistral-small-latest and 188/min on ministral-8b (2026-09).
        ProviderInfo(
            "mistral", "Mistral", "llm", base_url="https://api.mistral.ai/v1",
            default_model="ministral-8b-latest",
            env_vars=("MISTRAL_API_KEY",), get_key_url="https://console.mistral.ai/api-keys",
        ),
        # Cerebras Inference: free trial tier, 5 requests per minute per model (checked 2026-09).
        ProviderInfo(
            "cerebras", "Cerebras", "llm", base_url="https://api.cerebras.ai/v1",
            default_model="qwen-3.8-27b",
            env_vars=("CEREBRAS_API_KEY",), get_key_url="https://cloud.cerebras.ai",
        ),
        ProviderInfo(
            "ollama", "Ollama (local)", "llm", base_url="http://localhost:11434/v1",
            default_model="llama3.2", embedding_model="nomic-embed-text", requires_key=False,
            env_vars=("OLLAMA_BASE_URL",), get_key_url="https://ollama.com/download",
        ),
        ProviderInfo(
            "openai", "OpenAI", "llm",
            default_model="gpt-4.1-mini", embedding_model="text-embedding-3-small",
            env_vars=("OPENAI_API_KEY",), get_key_url="https://platform.openai.com/api-keys",
        ),
        ProviderInfo(
            "anthropic", "Anthropic Claude", "llm",
            default_model="claude-opus-5",
            env_vars=("ANTHROPIC_API_KEY",), get_key_url="https://console.anthropic.com/settings/keys",
        ),
        # Any other OpenAI-compatible endpoint: the user supplies the base URL, the model, and
        # (if the endpoint needs one) a key. The base URL is subject to the SSRF guard.
        ProviderInfo(
            "custom", "Custom (OpenAI-compatible)", "llm", requires_key=False,
            env_vars=("CUSTOM_OPENAI_BASE_URL", "CUSTOM_OPENAI_API_KEY", "CUSTOM_OPENAI_MODEL"),
        ),
        ProviderInfo("mock", "Mock (no network)", "llm", default_model="mock", requires_key=False),
    )
}

EMAIL_PROVIDERS: dict[str, ProviderInfo] = {
    "gmail": ProviderInfo(
        "gmail", "Gmail (SMTP + IMAP)", "email",
        env_vars=("SMTP_USER", "SMTP_PASSWORD"),
        get_key_url="https://myaccount.google.com/apppasswords",
    ),
    "mock": ProviderInfo("mock", "Mock (nothing is sent)", "email", requires_key=False),
}

# Notifications. Telegram: a bot token from @BotFather (plus an optional default chat id).
# Discord: a channel's webhook URL, which is itself the secret.
MESSAGING_PROVIDERS: dict[str, ProviderInfo] = {
    "telegram": ProviderInfo(
        "telegram", "Telegram bot", "messaging",
        base_url="https://api.telegram.org",
        env_vars=("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
        get_key_url="https://t.me/BotFather",
    ),
    "discord": ProviderInfo(
        "discord", "Discord webhook", "messaging",
        env_vars=("DISCORD_WEBHOOK_URL",),
        get_key_url="https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks",
    ),
}

# Web search. DuckDuckGo (through the ddgs library) needs no key; Tavily has a free tier.
SEARCH_PROVIDERS: dict[str, ProviderInfo] = {
    "duckduckgo": ProviderInfo("duckduckgo", "DuckDuckGo", "search", requires_key=False),
    "tavily": ProviderInfo(
        "tavily", "Tavily", "search", base_url="https://api.tavily.com",
        env_vars=("TAVILY_API_KEY",), get_key_url="https://app.tavily.com/home",
    ),
}

LLM_PROVIDER_NAMES = tuple(LLM_PROVIDERS)
EMAIL_PROVIDER_NAMES = tuple(EMAIL_PROVIDERS)

LLMProviderName = Literal[
    "gemini", "groq", "openrouter", "mistral", "cerebras", "ollama", "openai", "anthropic", "custom", "mock"
]
SearchProviderName = Literal["duckduckgo", "tavily"]

# Workspace apps: a token each, stored like any other credential.
WORKSPACE_PROVIDERS: dict[str, ProviderInfo] = {
    "notion": ProviderInfo(
        "notion", "Notion", "workspace", base_url="https://api.notion.com/v1",
        env_vars=("NOTION_API_KEY",), get_key_url="https://www.notion.so/profile/integrations",
    ),
    "airtable": ProviderInfo(
        "airtable", "Airtable", "workspace", base_url="https://api.airtable.com/v0",
        env_vars=("AIRTABLE_API_KEY",), get_key_url="https://airtable.com/create/tokens",
    ),
}
EmailProviderName = Literal["gmail", "mock"]
TelegramProviderName = Literal["telegram", "mock"]
DiscordProviderName = Literal["discord", "mock"]


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if isinstance(value, str) and not value.strip():
        return None
    return value.strip() if isinstance(value, str) else value


class LLMAccount(BaseModel):
    """How to reach one LLM provider. Blank values fall back to the catalog defaults."""

    api_key: SecretStr | None = None
    base_url: str | None = None
    model: str | None = None
    embedding_model: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Any:
        return _blank_to_none(value)


class EmailAccount(BaseModel):
    """An SMTP + IMAP mailbox. Defaults target Gmail with an App Password."""

    username: str | None = None
    password: SecretStr | None = None
    from_name: str | None = None
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(default=587, ge=1, le=65535)
    # auto: implicit TLS on port 465, STARTTLS otherwise.
    smtp_security: Literal["auto", "starttls", "ssl"] = "auto"
    imap_host: str = "imap.gmail.com"
    imap_port: int = Field(default=993, ge=1, le=65535)
    timeout_seconds: float = Field(default=30, gt=0)

    @field_validator("*", mode="before")
    @classmethod
    def _blank(cls, value: Any, info: Any) -> Any:
        value = _blank_to_none(value)
        if value is None and cls.model_fields[info.field_name].default is not None:
            return cls.model_fields[info.field_name].default
        return value

    @property
    def configured(self) -> bool:
        return bool(self.username) and self.password is not None

    @property
    def resolved_smtp_security(self) -> Literal["starttls", "ssl"]:
        if self.smtp_security == "auto":
            return "ssl" if self.smtp_port == 465 else "starttls"
        return self.smtp_security


class TelegramAccount(BaseModel):
    """A Telegram bot. `chat_id` is where a Telegram node sends when it names no chat."""

    bot_token: SecretStr | None = None
    chat_id: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Any:
        value = _blank_to_none(value)
        return str(value) if isinstance(value, int) else value

    @property
    def configured(self) -> bool:
        return self.bot_token is not None


class SearchAccount(BaseModel):
    api_key: SecretStr | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Any:
        return _blank_to_none(value)


class SpeechSettings(BaseModel):
    """Speech-to-text: Groq Whisper (the groq account's key) and local faster-whisper."""

    groq_model: str = "whisper-large-v3-turbo"
    # whisper-large-v3-turbo can't translate; translation uses this one.
    groq_translate_model: str = "whisper-large-v3"
    # Groq's upload limit (25 MB on the free tier, 100 MB on the dev tier).
    groq_max_file_mb: float = Field(default=25, gt=0)
    # faster-whisper model size (tiny, base, small, medium, large-v3, turbo), downloaded on
    # first use into models_dir.
    local_model: str = "base"
    models_dir: str | None = None
    local_compute_type: str = "int8"

    @field_validator("*", mode="before")
    @classmethod
    def _blank(cls, value: Any, info: Any) -> Any:
        value = _blank_to_none(value)
        if value is None and cls.model_fields[info.field_name].default is not None:
            return cls.model_fields[info.field_name].default
        return value


class DiscordAccount(BaseModel):
    """A Discord channel webhook (https://discord.com/api/webhooks/<id>/<token>)."""

    webhook_url: SecretStr | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @property
    def configured(self) -> bool:
        return self.webhook_url is not None


class ProviderSettings(BaseModel):
    """Credentials and defaults for every provider.

    `testing=True` (the pytest suite, via TESTING=true) makes the factory hand out mock
    providers for everything. Outside tests a provider without credentials is an error,
    never a silent mock.
    """

    testing: bool = False
    gemini: LLMAccount = Field(default_factory=LLMAccount)
    groq: LLMAccount = Field(default_factory=LLMAccount)
    openrouter: LLMAccount = Field(default_factory=LLMAccount)
    mistral: LLMAccount = Field(default_factory=LLMAccount)
    cerebras: LLMAccount = Field(default_factory=LLMAccount)
    ollama: LLMAccount = Field(default_factory=LLMAccount)
    openai: LLMAccount = Field(default_factory=LLMAccount)
    anthropic: LLMAccount = Field(default_factory=LLMAccount)
    custom: LLMAccount = Field(default_factory=LLMAccount)
    gmail: EmailAccount = Field(default_factory=EmailAccount)
    telegram: TelegramAccount = Field(default_factory=TelegramAccount)
    discord: DiscordAccount = Field(default_factory=DiscordAccount)
    tavily: SearchAccount = Field(default_factory=SearchAccount)
    # Workspace apps (a token in api_key, like Tavily's key).
    notion: SearchAccount = Field(default_factory=SearchAccount)
    airtable: SearchAccount = Field(default_factory=SearchAccount)
    speech: SpeechSettings = Field(default_factory=SpeechSettings)
    # The SSRF guard for user-supplied endpoints (the custom LLM's base URL), like the HTTP
    # Request node's: on unless HTTP_ALLOW_PRIVATE_NETWORKS is true.
    allow_private_network: bool = False
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    request_timeout_seconds: float = Field(default=60, gt=0)

    # --- lookups -------------------------------------------------------------------

    def llm_account(self, name: str) -> LLMAccount:
        if name not in LLM_PROVIDERS or name == "mock":
            raise ValueError(f"Unknown LLM provider '{name}'")
        return getattr(self, name)

    def email_account(self, name: str) -> EmailAccount:
        if name != "gmail":
            raise ValueError(f"Unknown email provider '{name}'")
        return self.gmail

    def default_model(self, name: str) -> str:
        info = LLM_PROVIDERS[name]
        if name == "mock":
            return "mock"
        return self.llm_account(name).model or info.default_model or ""

    def base_url(self, name: str) -> str | None:
        return self.llm_account(name).base_url or LLM_PROVIDERS[name].base_url

    def embedding_model(self, name: str) -> str | None:
        return self.llm_account(name).embedding_model or LLM_PROVIDERS[name].embedding_model

    def has_credentials(self, name: str) -> bool:
        """Whether a run could use provider `name` (always true in testing / for mocks)."""
        if self.testing or name == "mock":
            return True
        if name == "custom":
            # A key is optional (some endpoints have none), but the endpoint and model aren't.
            return bool(self.custom.base_url and self.custom.model)
        if name in LLM_PROVIDERS:
            return not LLM_PROVIDERS[name].requires_key or self.llm_account(name).api_key is not None
        if name in EMAIL_PROVIDERS:
            return self.email_account(name).configured
        if name == "telegram":
            return self.telegram.configured
        if name == "discord":
            return self.discord.configured
        if name == "duckduckgo":
            return True
        if name == "tavily":
            return self.tavily.api_key is not None
        if name in WORKSPACE_PROVIDERS:
            return getattr(self, name).api_key is not None
        return False

    def with_account(self, name: str, **fields: Any) -> ProviderSettings:
        """A copy with provider `name`'s account fields overridden (blank values ignored)."""
        current = getattr(self, name)
        updates = {key: value for key, value in fields.items() if _blank_to_none(value) is not None}
        account = type(current).model_validate({**current.model_dump(), **updates})
        return self.model_copy(update={name: account})

    # --- construction --------------------------------------------------------------

    @classmethod
    def from_mapping(cls, env: Mapping[str, Any]) -> ProviderSettings:
        """Build from environment-style keys (GEMINI_API_KEY, SMTP_USER, ...)."""

        def get(*names: str) -> Any:
            for name in names:
                value = _blank_to_none(env.get(name))
                if value is not None:
                    return value
            return None

        def llm(prefix: str, *, key: bool = True, base_url: bool = False, embedding: bool = False) -> dict[str, Any]:
            account: dict[str, Any] = {"model": get(f"{prefix}_MODEL")}
            if key:
                account["api_key"] = get(f"{prefix}_API_KEY", *(("GOOGLE_API_KEY",) if prefix == "GEMINI" else ()))
            if base_url:
                account["base_url"] = get(f"{prefix}_BASE_URL")
            if embedding:
                account["embedding_model"] = get(f"{prefix}_EMBEDDING_MODEL")
            return account

        gmail = {
            "username": get("SMTP_USER"),
            "password": get("SMTP_PASSWORD"),
            "from_name": get("SMTP_FROM_NAME"),
            "smtp_host": get("SMTP_HOST"),
            "smtp_port": get("SMTP_PORT"),
            "smtp_security": get("SMTP_SECURITY"),
            "imap_host": get("IMAP_HOST"),
            "imap_port": get("IMAP_PORT"),
        }
        retry = {
            "max_retries": get("LLM_MAX_RETRIES"),
            "base_delay": get("LLM_RETRY_BASE_DELAY_SECONDS"),
            "max_delay": get("LLM_RETRY_MAX_DELAY_SECONDS"),
        }
        values: dict[str, Any] = {
            "testing": str(get("TESTING") or "").lower() in ("1", "true", "yes", "on"),
            "gemini": llm("GEMINI", embedding=True),
            "groq": llm("GROQ"),
            "openrouter": llm("OPENROUTER"),
            "mistral": llm("MISTRAL"),
            "cerebras": llm("CEREBRAS"),
            "ollama": llm("OLLAMA", key=False, base_url=True, embedding=True),
            "openai": llm("OPENAI", base_url=True),
            "anthropic": llm("ANTHROPIC"),
            "custom": {
                "api_key": get("CUSTOM_OPENAI_API_KEY"),
                "base_url": get("CUSTOM_OPENAI_BASE_URL"),
                "model": get("CUSTOM_OPENAI_MODEL"),
            },
            "tavily": {"api_key": get("TAVILY_API_KEY")},
            "notion": {"api_key": get("NOTION_API_KEY")},
            "airtable": {"api_key": get("AIRTABLE_API_KEY")},
            "speech": {
                "groq_model": get("GROQ_WHISPER_MODEL"),
                "groq_translate_model": get("GROQ_WHISPER_TRANSLATE_MODEL"),
                "groq_max_file_mb": get("GROQ_WHISPER_MAX_FILE_MB"),
                "local_model": get("FASTER_WHISPER_MODEL"),
                "models_dir": get("WHISPER_MODELS_DIR"),
                "local_compute_type": get("FASTER_WHISPER_COMPUTE_TYPE"),
            },
            "allow_private_network": str(get("HTTP_ALLOW_PRIVATE_NETWORKS") or "").lower() in ("1", "true", "yes", "on"),
            "gmail": gmail,
            "telegram": {"bot_token": get("TELEGRAM_BOT_TOKEN"), "chat_id": get("TELEGRAM_CHAT_ID")},
            "discord": {"webhook_url": get("DISCORD_WEBHOOK_URL")},
            "retry": {k: v for k, v in retry.items() if v is not None},
        }
        timeout = get("LLM_REQUEST_TIMEOUT_SECONDS")
        if timeout is not None:
            values["request_timeout_seconds"] = timeout
        return cls.model_validate(values)

    @classmethod
    def from_env(cls) -> ProviderSettings:
        return cls.from_mapping(os.environ)


def missing_credentials_hint(name: str) -> str:
    if name == "custom":
        return (
            "no endpoint configured. Set CUSTOM_OPENAI_BASE_URL and CUSTOM_OPENAI_MODEL (and CUSTOM_OPENAI_API_KEY "
            "if it needs one) on the server, or connect a custom credential under Integrations"
        )
    if name == "tavily":
        return "no API key configured. Set TAVILY_API_KEY on the server, or connect a tavily credential under Integrations"
    if name == "notion":
        return (
            "no integration token configured. Create an internal integration at notion.so/profile/integrations, "
            "share the database with it, then set NOTION_API_KEY on the server or connect a notion credential under Integrations"
        )
    if name == "airtable":
        return (
            "no personal access token configured. Create one at airtable.com/create/tokens (scopes data.records:read "
            "and data.records:write, with the base added), then set AIRTABLE_API_KEY on the server or connect an "
            "airtable credential under Integrations"
        )
    if name == "telegram":
        return (
            "no bot token configured. Create a bot with @BotFather, then set TELEGRAM_BOT_TOKEN on the "
            "server or connect a telegram credential under Integrations"
        )
    if name == "discord":
        return (
            "no webhook URL configured. Create one in the channel's settings (Integrations > Webhooks), then "
            "set DISCORD_WEBHOOK_URL on the server, connect a discord credential under Integrations, or put "
            "the URL in the node's webhook_url"
        )
    if name == "gmail":
        return (
            "no email account configured. Set SMTP_USER and SMTP_PASSWORD (a Google App Password) "
            "on the server, or connect a gmail credential under Integrations"
        )
    env = LLM_PROVIDERS[name].env_vars[0] if name in LLM_PROVIDERS and LLM_PROVIDERS[name].env_vars else "its API key"
    return f"no API key configured. Set {env} on the server, or connect a {name} credential under Integrations"
