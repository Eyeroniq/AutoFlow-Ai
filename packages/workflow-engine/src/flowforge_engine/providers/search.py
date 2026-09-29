"""Web search providers: DuckDuckGo (through the ddgs library, no key) and Tavily (API key).

Both return [{"title", "url", "snippet"}]. Failures are ProviderErrors with a message that
says what to do: DuckDuckGo rate limits by IP without a documented quota, and Tavily's free
plan has a monthly credit allowance (HTTP 432 when it's used up).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from flowforge_engine.errors import ProviderError
from flowforge_engine.providers.retry import RetryPolicy, classify_status, parse_retry_after, redact, with_retries

TAVILY_BASE_URL = "https://api.tavily.com"
# Tavily and ddgs name time ranges differently.
_DDGS_TIME = {"day": "d", "week": "w", "month": "m", "year": "y"}
_DDGS_SAFE = {"on": "on", "moderate": "moderate", "off": "off"}


def _result(title: Any, url: Any, snippet: Any) -> dict[str, str] | None:
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    return {"title": str(title or "").strip() or url, "url": url, "snippet": " ".join(str(snippet or "").split())}


class DuckDuckGoSearch:
    is_mock = False

    def __init__(self, *, name: str = "duckduckgo", timeout: float = 15, ddgs_factory: Callable[..., Any] | None = None):
        self.name = name
        self._timeout = timeout
        self._factory = ddgs_factory

    def _search_sync(self, query: str, max_results: int, region: str, safe_search: str, time_range: str | None) -> list[Any]:
        try:
            from ddgs import DDGS
            from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException
        except ImportError as exc:  # pragma: no cover - depends on installed extras
            raise ProviderError(self.name, "the ddgs package isn't installed (flowforge-workflow-engine[sources])") from exc
        factory = self._factory or DDGS
        try:
            return factory(timeout=self._timeout).text(
                query, region=region, safesearch=_DDGS_SAFE.get(safe_search, "moderate"),
                timelimit=_DDGS_TIME.get(time_range or ""), max_results=max_results, backend="duckduckgo",
            ) or []
        except RatelimitException as exc:
            raise ProviderError(
                self.name,
                f"DuckDuckGo is rate limiting this server ({exc}); wait a minute, or set TAVILY_API_KEY so searches "
                "fall back to Tavily",
                status_code=429,
            ) from exc
        except TimeoutException as exc:
            raise ProviderError(self.name, f"DuckDuckGo didn't answer within {self._timeout:g}s", retryable=True) from exc
        except DDGSException as exc:
            if "no results" in str(exc).lower():
                return []
            raise ProviderError(self.name, f"DuckDuckGo search failed: {exc}") from exc

    async def search(
        self, query: str, *, max_results: int = 5, region: str = "wt-wt", safe_search: str = "moderate",
        time_range: str | None = None,
    ) -> list[dict[str, str]]:
        raw = await asyncio.to_thread(self._search_sync, query, max_results, region, safe_search, time_range)
        results = [_result(r.get("title"), r.get("href") or r.get("url"), r.get("body")) for r in raw if isinstance(r, dict)]
        return [r for r in results if r][:max_results]


class TavilySearch:
    is_mock = False

    def __init__(
        self,
        api_key: str,
        *,
        name: str = "tavily",
        retry: RetryPolicy | None = None,
        timeout: float = 30,
        base_url: str = TAVILY_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.name = name
        self._key = api_key
        self._retry = retry or RetryPolicy()
        self._timeout = timeout
        self._base = base_url.rstrip("/")
        self._transport = transport
        self._sleep = sleep

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        async def once() -> dict[str, Any]:
            try:
                async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                    response = await client.request(
                        method, f"{self._base}{path}", json=body, headers={"Authorization": f"Bearer {self._key}"}
                    )
            except httpx.TimeoutException as exc:
                raise ProviderError(self.name, f"no answer within {self._timeout:g}s", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise ProviderError(self.name, f"network error: {type(exc).__name__}: {redact(str(exc), self._key)}",
                                    retryable=True) from exc
            if response.status_code < 400:
                try:
                    return response.json()
                except ValueError:
                    raise ProviderError(self.name, "the response wasn't JSON") from None
            try:
                detail = response.json().get("detail")
                message = str(detail.get("error") if isinstance(detail, dict) else detail or response.text[:300])
            except (ValueError, AttributeError):
                message = response.text[:300]
            message = redact(message, self._key)
            if response.status_code == 432:
                raise ProviderError(self.name, f"the plan's credits are used up (HTTP 432): {message}", status_code=432)
            if response.status_code == 433:
                raise ProviderError(self.name, f"the pay-as-you-go limit is reached (HTTP 433): {message}", status_code=433)
            raise classify_status(self.name, response.status_code, message, retry_after=parse_retry_after(response.headers))

        return await with_retries(once, self._retry, sleep=self._sleep)

    async def search(
        self, query: str, *, max_results: int = 5, region: str = "wt-wt", safe_search: str = "moderate",
        time_range: str | None = None,
    ) -> list[dict[str, str]]:
        body: dict[str, Any] = {"query": query, "max_results": max_results, "search_depth": "basic", "include_answer": False}
        if time_range:
            body["time_range"] = time_range
        data = await self._request("POST", "/search", body)
        results = [_result(r.get("title"), r.get("url"), r.get("content")) for r in data.get("results", [])]
        return [r for r in results if r][:max_results]

    async def verify(self) -> dict[str, Any]:
        """GET /usage: proves the key and shows the credits left, without searching."""
        data = await self._request("GET", "/usage")
        key, account = data.get("key") or {}, data.get("account") or {}
        return {
            "plan": account.get("current_plan"),
            "credits_used": account.get("plan_usage", key.get("usage")),
            "credits_limit": account.get("plan_limit", key.get("limit")),
        }


class MockSearch:
    """Canned results (the test suite and provider "mock"); nothing is searched."""

    is_mock = True

    def __init__(self, name: str = "mock"):
        self.name = name

    async def search(self, query: str, *, max_results: int = 5, **_: Any) -> list[dict[str, str]]:
        return [
            {"title": f"Result {i} for {query}", "url": f"https://example.com/{i}", "snippet": f"Mock snippet {i} about {query}."}
            for i in range(1, max_results + 1)
        ]

    async def verify(self) -> dict[str, Any]:
        return {"mock": True}
