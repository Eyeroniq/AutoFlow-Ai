"""Posting to a Discord channel through a webhook (no bot, no OAuth).

POST <webhook_url>?wait=true with {"content": ..., "embeds": [...]} returns the created
message. A 429 answers {"retry_after": <seconds, float>, "global": bool} (plus Retry-After
headers); the retry helper waits that long and tries again. The webhook URL contains its
token, so it is redacted from every error message.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from flowforge_engine.errors import ProviderError
from flowforge_engine.providers.retry import RetryPolicy, redact, with_retries

MAX_CONTENT_CHARS = 2000
MAX_EMBED_TITLE = 256
MAX_EMBED_DESCRIPTION = 4096

_WEBHOOK_URL = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/(?P<id>\d+)/(?P<token>[\w-]+)/?$"
)


def parse_webhook_url(url: str) -> tuple[str, str]:
    """(webhook id, token) of a Discord webhook URL. Raises ValueError for anything else,
    so a Discord node can't be pointed at an arbitrary host."""
    match = _WEBHOOK_URL.match(url.strip())
    if not match:
        raise ValueError(
            "not a Discord webhook URL (expected https://discord.com/api/webhooks/<id>/<token>; copy it from "
            "the channel's Integrations > Webhooks)"
        )
    return match.group("id"), match.group("token")


def _retry_after(response: httpx.Response, data: dict[str, Any]) -> float | None:
    for value in (data.get("retry_after"), response.headers.get("x-ratelimit-reset-after"), response.headers.get("retry-after")):
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


class DiscordWebhookProvider:
    is_mock = False

    def __init__(
        self,
        webhook_url: str,
        *,
        name: str = "discord",
        retry: RetryPolicy | None = None,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.webhook_id, self._token = parse_webhook_url(webhook_url)
        self.name = name
        self._url = webhook_url.strip().rstrip("/")
        self._retry = retry or RetryPolicy()
        self._timeout = timeout
        self._transport = transport
        self._sleep = sleep

    def _clean(self, text: str) -> str:
        return redact(text, self._url, self._token)

    async def _once(self, method: str, params: dict[str, str], payload: dict[str, Any] | None) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.request(method, self._url, params=params, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, f"no answer within {self._timeout:g}s", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"network error: {type(exc).__name__}: {self._clean(str(exc))}", retryable=True) from exc
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        status = response.status_code
        if status < 300:
            return data
        message = self._clean(str(data.get("message") or response.text[:300] or f"HTTP {status}"))
        if status == 429:
            scope = "globally" if data.get("global") else "on this webhook"
            raise ProviderError(
                self.name, f"rate limited {scope} (HTTP 429): {message}",
                status_code=status, retryable=True, retry_after=_retry_after(response, data),
            )
        if status >= 500:
            raise ProviderError(self.name, f"server error (HTTP {status}): {message}", status_code=status, retryable=True)
        if status in (401, 404):
            raise ProviderError(
                self.name,
                f"the webhook was rejected (HTTP {status}: {message}); it may have been deleted. Create a new one in "
                "the channel's Integrations > Webhooks",
                status_code=status,
            )
        errors = data.get("errors")
        detail = f" {self._clean(str(errors))[:500]}" if errors else ""
        raise ProviderError(self.name, f"Discord refused the message (HTTP {status}): {message}.{detail}", status_code=status)

    async def execute(self, payload: dict[str, Any], *, thread_id: str | None = None) -> dict[str, Any]:
        """Post a message; returns Discord's Message object (?wait=true)."""
        params = {"wait": "true"}
        if thread_id:
            params["thread_id"] = thread_id
        return await with_retries(lambda: self._once("POST", params, payload), self._retry, sleep=self._sleep)

    async def verify(self) -> dict[str, Any]:
        """GET the webhook: proves it exists without posting anything."""
        data = await with_retries(lambda: self._once("GET", {}, None), self._retry, sleep=self._sleep)
        return {"webhook": data.get("name"), "channel_id": data.get("channel_id"), "guild_id": data.get("guild_id")}
