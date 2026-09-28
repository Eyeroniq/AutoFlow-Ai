from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Local runs read the repo-root .env (from apps/api) and optionally apps/api/.env.
    # In Docker, values come from the container environment instead.
    model_config = SettingsConfigDict(
        env_file=("../../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    PROJECT_NAME: str = "FlowForge AI"
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"

    DATABASE_URL: str = "postgresql+asyncpg://flowforge:flowforge@localhost:5433/flowforge"
    REDIS_URL: str = "redis://localhost:6379/0"

    # Required — no default, so a missing secret fails at startup instead of silently
    # signing tokens with a well-known value.
    JWT_SECRET: str = Field(min_length=32)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Comma-separated list of allowed browser origins.
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"

    # slowapi limit string applied to /auth/login, /auth/register, and /auth/refresh.
    AUTH_RATE_LIMIT: str = "10/minute"

    # LLM provider keys. Unset or blank -> that provider runs in mock mode.
    GEMINI_API_KEY: SecretStr | None = None
    OPENAI_API_KEY: SecretStr | None = None
    ANTHROPIC_API_KEY: SecretStr | None = None

    # Upper bound for a single node during a (synchronous) workflow run.
    WORKFLOW_NODE_TIMEOUT_SECONDS: float = Field(default=120, gt=0)

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
