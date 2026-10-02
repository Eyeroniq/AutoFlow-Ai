"""PUBLIC_DEMO mode: a deployment anyone can try without being able to touch the owner's accounts.

Off by default. When `PUBLIC_DEMO=true`, every account except `DEMO_OWNER_EMAIL` is a *visitor*:

- **AI is provided by the server.** Visitors never pick or bring a key: every AI provider (Gemini, Groq,
  OpenRouter, Mistral, Cerebras, OpenAI, Anthropic, Ollama, Custom) and web search silently use the
  server's own keys from `.env`, within a per-user daily cap on runs (`DEMO_RUNS_PER_DAY`) and on
  estimated tokens (`DEMO_TOKENS_PER_DAY`).
- **Personal accounts are never lent.** Gmail, Discord, Telegram, Notion and Airtable are the owner's
  real inboxes, servers, bots and workspaces. A visitor's node can use one only after the visitor
  connects *their own*; the server-wide `SMTP_*`, `TELEGRAM_*`, `DISCORD_WEBHOOK_URL`, `NOTION_API_KEY`
  and `AIRTABLE_API_KEY` are removed from the settings a visitor's runs, tests and triggers are built
  from. This is a deliberate safety boundary, not a missing feature.
- **The bots belong to the owner.** The Discord voice recorder and the Telegram Command Center only act
  for the owner's account (and refuse to start in demo mode without `DEMO_OWNER_EMAIL`).

The owner account keeps working exactly as without demo mode (its own credentials, the server accounts,
no caps). Everything funnels through `credentials.build_execution_services`, which is where this applies.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from flowforge_engine import ExecutionServices, ProviderError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import new_redis
from app.models.user import User

logger = logging.getLogger(__name__)

# The providers backed by someone's real accounts. In demo mode a visitor connects their own or has none.
PERSONAL_PROVIDERS = frozenset({"gmail", "discord", "telegram", "notion", "airtable"})
# The server-wide settings that hold those accounts (see ProviderSettings.from_mapping).
PERSONAL_ENV_KEYS = frozenset({
    "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM_NAME", "SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "IMAP_HOST", "IMAP_PORT",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL", "NOTION_API_KEY", "AIRTABLE_API_KEY",
})
USAGE_KEY = "flowforge:demo:{kind}:{user}:{day}"
USAGE_TTL_SECONDS = 3 * 86400


class DemoLimitReached(Exception):
    """A visitor used up today's demo allowance."""


def is_demo() -> bool:
    return settings.PUBLIC_DEMO


def owner_email() -> str:
    return settings.DEMO_OWNER_EMAIL.strip().lower()


def is_owner_email(email: str | None) -> bool:
    """Outside demo mode everyone is their own boss; in it, only the configured owner is."""
    if not is_demo():
        return True
    return bool(email) and bool(owner_email()) and (email or "").strip().lower() == owner_email()


def is_visitor(user: User) -> bool:
    return is_demo() and not is_owner_email(user.email)


async def owner_user_id(db: AsyncSession) -> uuid.UUID | None:
    if not owner_email():
        return None
    return await db.scalar(select(User.id).where(func.lower(User.email) == owner_email()))


async def user_is_visitor(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Whether the account with this id is a visitor (False outside demo mode)."""
    if not is_demo():
        return False
    return (await owner_user_id(db)) != user_id


# --- Usage caps ---------------------------------------------------------------------------------------


def _key(kind: str, user_id: uuid.UUID) -> str:
    return USAGE_KEY.format(kind=kind, user=user_id, day=datetime.now(UTC).strftime("%Y-%m-%d"))


async def _incr(kind: str, user_id: uuid.UUID, amount: int) -> int:
    redis = new_redis()
    try:
        key = _key(kind, user_id)
        total = int(await redis.incrby(key, amount))
        await redis.expire(key, USAGE_TTL_SECONDS)
        return total
    finally:
        await redis.aclose()


async def _read(kind: str, user_id: uuid.UUID) -> int:
    redis = new_redis()
    try:
        return int(await redis.get(_key(kind, user_id)) or 0)
    finally:
        await redis.aclose()


async def charge_run(user_id: uuid.UUID) -> None:
    """Count one run against today's allowance; raise DemoLimitReached when it is used up."""
    if await _incr("runs", user_id, 1) > settings.DEMO_RUNS_PER_DAY:
        raise DemoLimitReached(
            f"The demo allows {settings.DEMO_RUNS_PER_DAY} runs a day per account and today's are used up. "
            "Try again tomorrow, or self-host FlowForge for no limits."
        )


async def usage(user_id: uuid.UUID) -> dict[str, int]:
    return {
        "runs": await _read("runs", user_id), "runs_limit": settings.DEMO_RUNS_PER_DAY,
        "tokens": await _read("tokens", user_id), "tokens_limit": settings.DEMO_TOKENS_PER_DAY,
    }


def estimate_tokens(*texts: str) -> int:
    """About four characters to a token: close enough to enforce a daily budget."""
    return max(1, sum(len(t) for t in texts) // 4)


class MeteredLLM:
    """A visitor's view of an AI provider: calls are refused once today's token budget is spent, and
    each call's estimated tokens are added to it."""

    def __init__(self, inner: Any, user_id: uuid.UUID):
        self._inner, self._user_id = inner, user_id
        self.is_mock = getattr(inner, "is_mock", False)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def _guard(self) -> None:
        if await _read("tokens", self._user_id) >= settings.DEMO_TOKENS_PER_DAY:
            raise ProviderError(
                "demo",
                f"The demo allows about {settings.DEMO_TOKENS_PER_DAY:,} AI tokens a day per account and today's are used up. "
                "Try again tomorrow, or self-host FlowForge for no limits.",
            )

    async def generate(self, system_prompt: str, user_prompt: str, *args: Any, **kwargs: Any) -> str:
        await self._guard()
        text = await self._inner.generate(system_prompt, user_prompt, *args, **kwargs)
        await _incr("tokens", self._user_id, estimate_tokens(system_prompt, user_prompt, text))
        return text

    async def generate_with_image(self, system_prompt: str, user_prompt: str, *args: Any, **kwargs: Any) -> str:
        await self._guard()
        text = await self._inner.generate_with_image(system_prompt, user_prompt, *args, **kwargs)
        await _incr("tokens", self._user_id, estimate_tokens(system_prompt, user_prompt, text) + 800)  # the image
        return text

    async def embed(self, texts: Any, *args: Any, **kwargs: Any) -> Any:
        await self._guard()
        result = await self._inner.embed(texts, *args, **kwargs)
        await _incr("tokens", self._user_id, estimate_tokens(*(texts if isinstance(texts, list) else [str(texts)])))
        return result

    async def stream(self, system_prompt: str, user_prompt: str, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        await self._guard()
        produced = 0
        async for chunk in self._inner.stream(system_prompt, user_prompt, *args, **kwargs):
            produced += len(chunk)
            yield chunk
        await _incr("tokens", self._user_id, estimate_tokens(system_prompt, user_prompt) + produced // 4)


class MeteredServices(ExecutionServices):
    """Execution services whose AI providers are metered for one visitor."""

    def __init__(self, user_id: uuid.UUID, **kwargs: Any):
        super().__init__(**kwargs)
        self._metered_for = user_id
        self._metered: dict[str, MeteredLLM] = {}

    def missing_credentials_message(self, provider_name: str) -> str:
        if provider_name in PERSONAL_PROVIDERS:
            label = {"gmail": "Gmail", "discord": "Discord", "telegram": "Telegram", "notion": "Notion", "airtable": "Airtable"}[provider_name]
            return (
                f"Authentication missing for provider '{provider_name}': this node needs your own {label} account. "
                f"Connect it under Integrations, or with the Connect button on the node. The demo's own {label} account "
                "is never available to visitors."
            )
        return super().missing_credentials_message(provider_name)

    def llm(self, provider_name: str) -> Any:
        if provider_name not in self._metered:
            self._metered[provider_name] = MeteredLLM(super().llm(provider_name), self._metered_for)
        return self._metered[provider_name]

