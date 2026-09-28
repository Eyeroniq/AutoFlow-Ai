from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Local runs read the repo-root .env (from apps/api) and optionally apps/api/.env.
    # In Docker, values come from the container environment instead.
    model_config = SettingsConfigDict(
        env_file=("../../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # `GROQ_API_KEY=` (blank) means "not set", including for typed fields like SMTP_PORT.
        env_ignore_empty=True,
    )

    PROJECT_NAME: str = "FlowForge AI"
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    # Set by the pytest suite: providers become mocks. Never set it in a real deployment.
    TESTING: bool = False

    DATABASE_URL: str = "postgresql+asyncpg://flowforge:flowforge@localhost:5433/flowforge"
    REDIS_URL: str = "redis://localhost:6379/0"

    # Required — no default, so a missing secret fails at startup instead of silently
    # signing tokens with a well-known value.
    JWT_SECRET: str = Field(min_length=32)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Required: Fernet key(s) that encrypt stored credentials. Comma-separate several to
    # rotate: the first encrypts, all of them decrypt.
    ENCRYPTION_KEY: SecretStr

    # Comma-separated list of allowed browser origins.
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"

    # slowapi limit string applied to /auth/login, /auth/register, and /auth/refresh.
    AUTH_RATE_LIMIT: str = "10/minute"

    # --- Providers: server-wide defaults. A user's own stored credential wins. ---------
    # Unset values fall back to the engine's defaults (see flowforge_engine.providers).
    GEMINI_API_KEY: SecretStr | None = None
    GEMINI_MODEL: str | None = None
    GEMINI_EMBEDDING_MODEL: str | None = None
    GROQ_API_KEY: SecretStr | None = None
    GROQ_MODEL: str | None = None
    OPENROUTER_API_KEY: SecretStr | None = None
    OPENROUTER_MODEL: str | None = None
    OLLAMA_BASE_URL: str | None = None
    OLLAMA_MODEL: str | None = None
    OLLAMA_EMBEDDING_MODEL: str | None = None
    OPENAI_API_KEY: SecretStr | None = None
    OPENAI_MODEL: str | None = None
    ANTHROPIC_API_KEY: SecretStr | None = None
    ANTHROPIC_MODEL: str | None = None

    # Gmail over SMTP/IMAP with an App Password (the same account sends and reads).
    SMTP_HOST: str | None = None
    SMTP_PORT: int | None = None
    SMTP_SECURITY: str | None = None
    SMTP_USER: str | None = None
    SMTP_PASSWORD: SecretStr | None = None
    SMTP_FROM_NAME: str | None = None
    IMAP_HOST: str | None = None
    IMAP_PORT: int | None = None

    # Backoff for 429/5xx/network errors; free-tier limits change, so nothing is hardcoded.
    LLM_MAX_RETRIES: int | None = Field(default=None, ge=0, le=10)
    LLM_RETRY_BASE_DELAY_SECONDS: float | None = Field(default=None, ge=0)
    LLM_RETRY_MAX_DELAY_SECONDS: float | None = Field(default=None, ge=0)
    LLM_REQUEST_TIMEOUT_SECONDS: float | None = Field(default=None, gt=0)

    # Upper bound for a single node during a workflow run.
    WORKFLOW_NODE_TIMEOUT_SECONDS: float = Field(default=120, gt=0)

    # --- Async execution (Celery) ----------------------------------------------------
    # Broker and result backend default to REDIS_URL.
    CELERY_BROKER_URL: str | None = None
    CELERY_RESULT_BACKEND: str | None = None
    # A whole run is stopped (and marked failed) after this long. Celery's own hard limit
    # sits a minute above it as a backstop.
    EXECUTION_TIME_LIMIT_SECONDS: float = Field(default=600, gt=0)
    # Retries for infrastructure errors (database/broker unreachable) before a run starts.
    # Node failures are never retried: they are recorded as the run's result.
    CELERY_TASK_MAX_RETRIES: int = Field(default=3, ge=0)
    # A running execution's worker writes a heartbeat this often; one silent for
    # EXECUTION_STALE_AFTER_SECONDS is treated as crashed and marked failed.
    EXECUTION_HEARTBEAT_SECONDS: float = Field(default=5, gt=0)
    EXECUTION_STALE_AFTER_SECONDS: float = Field(default=30, gt=0)
    # A pending execution no worker picked up within this long is marked failed.
    EXECUTION_PENDING_TIMEOUT_SECONDS: float = Field(default=3600, gt=0)
    # How often the API sweeps for stale executions.
    EXECUTION_RECOVERY_INTERVAL_SECONDS: float = Field(default=15, gt=0)
    # POST /stop waits this long for the worker to confirm before answering 202.
    EXECUTION_STOP_WAIT_SECONDS: float = Field(default=5, ge=0)
    # How often a running execution checks for a stop request.
    EXECUTION_STOP_POLL_SECONDS: float = Field(default=0.25, gt=0)

    # --- WebSocket ---------------------------------------------------------------------
    WS_HEARTBEAT_SECONDS: float = Field(default=15, gt=0)
    WS_AUTH_TIMEOUT_SECONDS: float = Field(default=10, gt=0)
    # Safety net: a watched execution's state is re-read from the database this often, so
    # a missed pub/sub message can't leave a client waiting forever.
    WS_DB_CHECK_SECONDS: float = Field(default=10, gt=0)

    @property
    def celery_broker_url(self) -> str:
        return self.CELERY_BROKER_URL or self.REDIS_URL

    @property
    def celery_result_backend(self) -> str:
        return self.CELERY_RESULT_BACKEND or self.REDIS_URL

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    def provider_env(self) -> dict[str, Any]:
        """The provider-related settings as environment-style keys, secrets unwrapped.

        Feeds flowforge_engine's ProviderSettings.from_mapping; never log or return it.
        """
        names = (
            "TESTING", "GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_EMBEDDING_MODEL", "GROQ_API_KEY",
            "GROQ_MODEL", "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "OLLAMA_BASE_URL", "OLLAMA_MODEL",
            "OLLAMA_EMBEDDING_MODEL", "OPENAI_API_KEY", "OPENAI_MODEL", "ANTHROPIC_API_KEY",
            "ANTHROPIC_MODEL", "SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_USER", "SMTP_PASSWORD",
            "SMTP_FROM_NAME", "IMAP_HOST", "IMAP_PORT", "LLM_MAX_RETRIES", "LLM_RETRY_BASE_DELAY_SECONDS",
            "LLM_RETRY_MAX_DELAY_SECONDS", "LLM_REQUEST_TIMEOUT_SECONDS",
        )
        env: dict[str, Any] = {}
        for name in names:
            value = getattr(self, name)
            env[name] = value.get_secret_value() if isinstance(value, SecretStr) else value
        env["TESTING"] = "true" if self.TESTING else ""
        return env

    def secret_values(self) -> list[str]:
        """Server secrets the log formatter redacts wherever they appear."""
        secrets = [self.JWT_SECRET, *self.ENCRYPTION_KEY.get_secret_value().split(",")]
        for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                     "ANTHROPIC_API_KEY", "SMTP_PASSWORD"):
            value = getattr(self, name)
            if value is not None:
                secrets.append(value.get_secret_value())
        return [s.strip() for s in secrets if s and s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
