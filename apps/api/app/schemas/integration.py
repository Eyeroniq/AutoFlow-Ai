from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

from app.models.enums import IntegrationStatus


class ConnectRequest(BaseModel):
    """Credential fields; which ones apply depends on the provider.

    - gemini / groq / openrouter / mistral / cerebras / anthropic: `api_key` (required), optional `model`
    - openai: `api_key`, optional `base_url` and `model`
    - custom (any OpenAI-compatible endpoint): `base_url` and `model` (required), `api_key` if it needs one
    - tavily: `api_key`
    - notion (an internal integration token) / airtable (a personal access token): `api_key`
    - ollama: optional `base_url` and `model` (no key)
    - gmail: `email` and `app_password` (a Google App Password), optional SMTP/IMAP overrides
    - telegram: `bot_token` (from @BotFather), optional `chat_id` (where Telegram nodes send by default)
    - discord: `webhook_url` (a channel webhook)
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"api_key": "AIza...your-key", "model": "gemini-3.8-flash"},
                {"email": "you@gmail.com", "app_password": "abcd efgh ijkl mnop"},
                {"base_url": "http://host.docker.internal:11434/v1", "model": "llama3.2"},
                {"bot_token": "123456789:AAE...from-BotFather", "chat_id": "123456789"},
            ]
        },
    )

    api_key: SecretStr | None = Field(default=None, description="Never returned; responses show it masked.")
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200, description="Default model for this provider.")
    email: EmailStr | None = None
    app_password: SecretStr | None = Field(default=None, description="Google App Password; never returned.")
    from_name: str | None = Field(default=None, max_length=200)
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_security: Literal["auto", "starttls", "ssl"] | None = None
    imap_host: str | None = Field(default=None, max_length=255)
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    bot_token: SecretStr | None = Field(default=None, description="Telegram bot token; never returned.")
    chat_id: str | None = Field(default=None, max_length=100, description="Telegram chat Telegram nodes send to by default.")
    webhook_url: SecretStr | None = Field(default=None, description="Discord webhook URL; never returned (shown masked).")

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return value.rstrip("/") if value else value


class IntegrationRead(BaseModel):
    provider: str
    label: str
    kind: Literal["llm", "email", "messaging", "search", "workspace"]
    # True when *you* have stored a credential (it takes priority over the server's).
    connected: bool
    # Which credential a run would use right now.
    source: Literal["user", "server", "none"]
    status: IntegrationStatus
    # Your stored credential with secrets masked, e.g. {"api_key": "AIz...9xQk"}.
    masked: dict[str, Any] | None = None
    connected_at: datetime | None = None
    last_test: dict[str, Any] | None = None
    default_model: str | None = None
    get_key_url: str | None = None


class IntegrationTestResult(BaseModel):
    provider: str
    success: bool
    source: Literal["user", "server", "none"]
    latency_ms: int
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    tested_at: datetime
