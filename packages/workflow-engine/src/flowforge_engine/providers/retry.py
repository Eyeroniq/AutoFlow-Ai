"""Exponential backoff for transient provider failures (HTTP 429/5xx, network errors).

SDK-level retries are switched off in every adapter so this is the single place that
decides how often and how long to wait. Free-tier limits change without notice, so
nothing here knows about quotas: it only honours what the provider says (Retry-After /
RetryInfo) and otherwise backs off exponentially.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pydantic import BaseModel, Field

from flowforge_engine.errors import ProviderError

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryPolicy(BaseModel):
    max_retries: int = Field(default=3, ge=0, le=10, description="Retries after the first attempt.")
    base_delay: float = Field(default=1.0, ge=0, description="Seconds before the first retry; doubles each time.")
    max_delay: float = Field(default=30.0, ge=0, description="Longest single wait, including a provider's Retry-After.")


def backoff_delay(retry_number: int, policy: RetryPolicy, retry_after: float | None = None) -> float | None:
    """Seconds to wait before retry number `retry_number` (1-based), or None to give up.

    A provider-supplied Retry-After wins; if it asks for longer than `max_delay` we give
    up immediately rather than hold a workflow node hostage (a fallback provider, if
    configured, can answer instead).
    """
    if retry_after is not None:
        if retry_after > policy.max_delay:
            return None
        return retry_after + random.uniform(0, 0.25)
    delay = min(policy.max_delay, policy.base_delay * 2 ** (retry_number - 1))
    # "Equal jitter": at least half the computed delay, so concurrent runs spread out.
    return delay / 2 + random.uniform(0, delay / 2)


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run `operation`, retrying retryable ProviderErrors with backoff.

    The final error says how many attempts were made, e.g.
    "groq: rate limited (HTTP 429): ... (gave up after 4 attempts)".
    """
    attempt = 1
    while True:
        try:
            return await operation()
        except ProviderError as exc:
            if not exc.retryable:
                raise
            if attempt > policy.max_retries:
                raise exc.gave_up(attempt) from exc.__cause__
            delay = backoff_delay(attempt, policy, exc.retry_after)
            if delay is None:
                raise exc.gave_up(
                    attempt,
                    f"provider asked to wait {exc.retry_after:.0f}s, longer than the "
                    f"{policy.max_delay:.0f}s retry limit",
                ) from exc.__cause__
            logger.warning(
                "provider call failed; retrying",
                extra={
                    "provider": exc.provider,
                    "status_code": exc.status_code,
                    "attempt": attempt,
                    "retry_in_seconds": round(delay, 2),
                    "error": str(exc),
                },
            )
            await sleep(delay)
            attempt += 1


def classify_status(
    provider: str,
    status: int,
    message: str,
    *,
    retry_after: float | None = None,
) -> ProviderError:
    """A ProviderError for an HTTP error status, flagged retryable for 408/429/5xx."""
    if status in (401, 403):
        return ProviderError(
            provider, f"authentication failed (HTTP {status}): {message} — check the API key", status_code=status
        )
    if status == 429:
        return ProviderError(
            provider, f"rate limited (HTTP 429): {message}", status_code=status, retryable=True, retry_after=retry_after
        )
    if status == 408 or status >= 500:
        return ProviderError(
            provider, f"server error (HTTP {status}): {message}", status_code=status, retryable=True,
            retry_after=retry_after,
        )
    return ProviderError(provider, f"API error {status}: {message}", status_code=status)


def parse_retry_after(headers: object) -> float | None:
    """Seconds from `retry-after-ms` / `retry-after` headers (integer or float seconds)."""
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = getter(name)
        if value is None:
            continue
        try:
            seconds = float(value) * scale
        except (TypeError, ValueError):
            continue  # HTTP-date form: rare for APIs, fall back to our own backoff
        if seconds >= 0:
            return seconds
    return None


def redact(text: str, *secrets: str | None) -> str:
    """Replace any of `secrets` that appear in `text` (e.g. an echoed API key)."""
    for secret in secrets:
        if secret and len(secret) >= 6 and secret in text:
            text = text.replace(secret, "[REDACTED]")
    return text
