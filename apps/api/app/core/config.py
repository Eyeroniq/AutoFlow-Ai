from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

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

    # --- Deployments (POST /api/v1/deployments/{id}/run) ------------------------------
    # Per deployment (every caller of one endpoint shares it), counted after the API key
    # is checked. The status endpoint has its own, higher limit for polling.
    DEPLOYMENT_RUN_RATE_LIMIT: str = "30/minute"
    DEPLOYMENT_STATUS_RATE_LIMIT: str = "240/minute"
    # ?wait=true blocks this long by default (?timeout= overrides, up to the maximum),
    # then answers 202 with the execution id if the run hasn't finished.
    DEPLOYMENT_WAIT_TIMEOUT_SECONDS: float = Field(default=30, gt=0)
    DEPLOYMENT_MAX_WAIT_SECONDS: float = Field(default=120, gt=0)

    # --- Providers: server-wide defaults. A user's own stored credential wins. ---------
    # Unset values fall back to the engine's defaults (see flowforge_engine.providers).
    GEMINI_API_KEY: SecretStr | None = None
    GEMINI_MODEL: str | None = None
    GEMINI_EMBEDDING_MODEL: str | None = None
    GROQ_API_KEY: SecretStr | None = None
    GROQ_MODEL: str | None = None
    OPENROUTER_API_KEY: SecretStr | None = None
    OPENROUTER_MODEL: str | None = None
    MISTRAL_API_KEY: SecretStr | None = None
    MISTRAL_MODEL: str | None = None
    CEREBRAS_API_KEY: SecretStr | None = None
    CEREBRAS_MODEL: str | None = None
    # Any OpenAI-compatible endpoint. Its base URL must be public unless HTTP_ALLOW_PRIVATE_NETWORKS.
    CUSTOM_OPENAI_BASE_URL: str | None = None
    CUSTOM_OPENAI_API_KEY: SecretStr | None = None
    CUSTOM_OPENAI_MODEL: str | None = None
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

    # Notifications. A Telegram bot token from @BotFather (plus the chat Telegram nodes send
    # to by default), and a Discord channel webhook URL. Users can store their own instead.
    # Web search: Tavily (optional; DuckDuckGo needs no key).
    TAVILY_API_KEY: SecretStr | None = None
    # Workspace apps: a Notion internal integration token and an Airtable personal access
    # token. Users can connect their own under Integrations instead.
    NOTION_API_KEY: SecretStr | None = None
    AIRTABLE_API_KEY: SecretStr | None = None

    # Speech-to-text. Groq Whisper uses GROQ_API_KEY; faster-whisper runs locally and keeps
    # its models in WHISPER_MODELS_DIR (a volume in Docker).
    GROQ_WHISPER_MODEL: str | None = None
    GROQ_WHISPER_TRANSLATE_MODEL: str | None = None
    GROQ_WHISPER_MAX_FILE_MB: float | None = Field(default=None, gt=0)
    FASTER_WHISPER_MODEL: str | None = None
    FASTER_WHISPER_COMPUTE_TYPE: str | None = None
    WHISPER_MODELS_DIR: str | None = None

    TELEGRAM_BOT_TOKEN: SecretStr | None = None
    TELEGRAM_CHAT_ID: str | None = None
    DISCORD_WEBHOOK_URL: SecretStr | None = None

    # Backoff for 429/5xx/network errors; free-tier limits change, so nothing is hardcoded.
    LLM_MAX_RETRIES: int | None = Field(default=None, ge=0, le=10)
    LLM_RETRY_BASE_DELAY_SECONDS: float | None = Field(default=None, ge=0)
    LLM_RETRY_MAX_DELAY_SECONDS: float | None = Field(default=None, ge=0)
    LLM_REQUEST_TIMEOUT_SECONDS: float | None = Field(default=None, gt=0)

    # Upper bound for a single node during a workflow run.
    WORKFLOW_NODE_TIMEOUT_SECONDS: float = Field(default=120, gt=0)

    # SSRF guard: the HTTP Request node refuses private/internal addresses unless this is
    # true. Only for local development (e.g. calling a service on your machine).
    HTTP_ALLOW_PRIVATE_NETWORKS: bool = False

    # --- Uploaded files (POST /api/files) --------------------------------------------
    # Where the bytes live. In Docker this is the `files_data` volume shared by the API
    # and every worker; for a local run it defaults to apps/api/.data/files.
    FILES_DIR: str = str(Path(__file__).resolve().parents[2] / ".data" / "files")
    MAX_UPLOAD_MB: float = Field(default=25, gt=0, le=1024)
    # Audio and video for speech-to-text (recordings are much larger than documents).
    MAX_MEDIA_UPLOAD_MB: float = Field(default=500, gt=0, le=4096)
    # Sample documents the seed loads (the repo's samples/; /samples in Docker).
    SAMPLES_DIR: str | None = None

    # --- Async execution (Celery) ----------------------------------------------------
    # Broker and result backend default to REDIS_URL.
    CELERY_BROKER_URL: str | None = None
    CELERY_RESULT_BACKEND: str | None = None
    # A whole run is stopped (and marked failed) after this long. Celery's own hard limit
    # sits a minute above it as a backstop. 30 minutes leaves room for Speech to Text on
    # long recordings (an hour of audio takes ~1 minute on Groq, 10+ on a CPU).
    EXECUTION_TIME_LIMIT_SECONDS: float = Field(default=1800, gt=0)
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

    # --- Triggers (schedules, email, webhook) ------------------------------------------
    # Celery beat ticks every minute; a schedule whose fire time is older than this when a
    # tick sees it (beat or the workers were down) is skipped instead of run late.
    TRIGGER_MISFIRE_GRACE_SECONDS: float = Field(default=3600, gt=0)
    # Email triggers poll at most this often (per trigger).
    EMAIL_TRIGGER_MIN_POLL_MINUTES: int = Field(default=1, ge=1)
    # Telegram Command Center (app.services.telegram_center, the telegram-listener service).
    TELEGRAM_RATE_LIMIT_PER_MINUTE: int = Field(default=10, ge=1, description="Requests per chat per minute.")
    TELEGRAM_CONFIRM_SECONDS: int = Field(default=300, ge=30, description="How long a Confirm button works.")
    TELEGRAM_RUN_WAIT_SECONDS: float = Field(default=600, gt=0, description="How long the bot waits for a run's result.")
    # Discord voice monitor (app.discord_bot, the discord-bot service). Without a token, guild,
    # and channel it idles.
    DISCORD_BOT_TOKEN: SecretStr | None = None
    DISCORD_MONITOR_GUILD_ID: int | None = Field(default=None, gt=0)
    DISCORD_MONITOR_CHANNEL_ID: int | None = Field(default=None, gt=0)
    DISCORD_RECORDING_MAX_MINUTES: float = Field(default=90, gt=0, description="Safety cap on one recording.")
    DISCORD_MIN_RECORDING_SECONDS: float = Field(default=30, ge=0, description="Shorter recordings aren't processed.")
    DISCORD_MEETING_PIPELINE: str = Field(default="Meeting Notes", description="Name of the pipeline that summarizes a recording.")
    DISCORD_RECORDER: Literal["node", "pycord"] = Field(
        default="node",
        description="node: the discord-recorder service (discord.js) records. pycord: app.discord_bot records itself (its voice receive is unreliable).",
    )
    DISCORD_OUTPUT: Literal["audio", "notes"] = Field(
        default="audio",
        description="audio: send the recording to Telegram as an audio file. notes: run the Meeting Notes pipeline on it.",
    )
    DISCORD_RUN_WAIT_SECONDS: float = Field(default=1800, gt=0, description="How long the bot waits for the summary run.")
    # Longest an email poll may hold its lock (a crashed poller frees it after this).
    EMAIL_POLL_LOCK_SECONDS: int = Field(default=300, gt=0)

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
    def samples_dir(self) -> Path:
        if self.SAMPLES_DIR:
            return Path(self.SAMPLES_DIR)
        parents = Path(__file__).resolve().parents  # apps/api/app/core -> the repo root is 4 up
        return parents[4] / "samples" if len(parents) > 4 else Path("/samples")

    @property
    def max_upload_bytes(self) -> int:
        return int(self.MAX_UPLOAD_MB * 1024 * 1024)

    @property
    def max_media_upload_bytes(self) -> int:
        return int(self.MAX_MEDIA_UPLOAD_MB * 1024 * 1024)

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    def provider_env(self) -> dict[str, Any]:
        """The provider-related settings as environment-style keys, secrets unwrapped.

        Feeds flowforge_engine's ProviderSettings.from_mapping; never log or return it.
        """
        names = (
            "TESTING", "GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_EMBEDDING_MODEL", "GROQ_API_KEY",
            "GROQ_MODEL", "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "MISTRAL_API_KEY", "MISTRAL_MODEL",
            "CEREBRAS_API_KEY", "CEREBRAS_MODEL", "CUSTOM_OPENAI_BASE_URL", "CUSTOM_OPENAI_API_KEY",
            "CUSTOM_OPENAI_MODEL", "TAVILY_API_KEY", "GROQ_WHISPER_MODEL", "GROQ_WHISPER_TRANSLATE_MODEL",
            "GROQ_WHISPER_MAX_FILE_MB", "FASTER_WHISPER_MODEL", "FASTER_WHISPER_COMPUTE_TYPE", "WHISPER_MODELS_DIR",
            "HTTP_ALLOW_PRIVATE_NETWORKS", "OLLAMA_BASE_URL", "OLLAMA_MODEL",
            "OLLAMA_EMBEDDING_MODEL", "OPENAI_API_KEY", "OPENAI_MODEL", "ANTHROPIC_API_KEY",
            "ANTHROPIC_MODEL", "SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_USER", "SMTP_PASSWORD",
            "SMTP_FROM_NAME", "IMAP_HOST", "IMAP_PORT", "LLM_MAX_RETRIES", "LLM_RETRY_BASE_DELAY_SECONDS",
            "LLM_RETRY_MAX_DELAY_SECONDS", "LLM_REQUEST_TIMEOUT_SECONDS", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
            "DISCORD_WEBHOOK_URL", "NOTION_API_KEY", "AIRTABLE_API_KEY",
        )
        env: dict[str, Any] = {}
        for name in names:
            value = getattr(self, name)
            env[name] = value.get_secret_value() if isinstance(value, SecretStr) else value
        env["TESTING"] = "true" if self.TESTING else ""
        env["HTTP_ALLOW_PRIVATE_NETWORKS"] = "true" if self.HTTP_ALLOW_PRIVATE_NETWORKS else ""
        return env

    def secret_values(self) -> list[str]:
        """Server secrets the log formatter redacts wherever they appear."""
        secrets = [self.JWT_SECRET, *self.ENCRYPTION_KEY.get_secret_value().split(",")]
        for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                     "ANTHROPIC_API_KEY", "SMTP_PASSWORD", "TELEGRAM_BOT_TOKEN", "DISCORD_WEBHOOK_URL", "DISCORD_BOT_TOKEN",
                     "MISTRAL_API_KEY", "CEREBRAS_API_KEY", "CUSTOM_OPENAI_API_KEY", "TAVILY_API_KEY",
                     "NOTION_API_KEY", "AIRTABLE_API_KEY"):
            value = getattr(self, name)
            if value is not None:
                secrets.append(value.get_secret_value())
        return [s.strip() for s in secrets if s and s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
