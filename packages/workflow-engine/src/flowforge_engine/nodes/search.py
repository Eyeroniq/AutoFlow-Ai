"""Web Search: DuckDuckGo (no key) or Tavily, falling back to the other on failure.

Results are [{title, url, snippet}]. With `fetch_pages`, the top results' pages are read
the same way the Web Page node reads one (fetch through the SSRF guard, trafilatura
extraction), so a search result pointing at an internal address is refused like any URL.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

from flowforge_engine.errors import ProviderError
from flowforge_engine.fetch import FetchError, fetch_url
from flowforge_engine.models import GraphNode, NodeContext, NodeResult
from flowforge_engine.nodes.feeds import WebPageConfig, extract_page
from flowforge_engine.providers.settings import SearchProviderName
from flowforge_engine.registry import NodeConfig, NodeDefinition, register_node
from flowforge_engine.textutil import clip
from flowforge_engine.variables import contains_reference

logger = logging.getLogger(__name__)

OTHER = {"duckduckgo": "tavily", "tavily": "duckduckgo"}


class WebSearchConfig(NodeConfig):
    query: str = Field(min_length=1, description="What to search for; references work, e.g. {{question.value}}.")
    provider: SearchProviderName = Field(
        default="duckduckgo", description="duckduckgo: no key needed. tavily: TAVILY_API_KEY (free monthly credits)."
    )
    fallback: bool = Field(
        default=True,
        description="If the provider fails (rate limited, down), try the other one: Tavily only when a key is configured.",
    )
    max_results: int = Field(default=5, ge=1, le=20)
    fetch_pages: int = Field(default=0, ge=0, le=10, description="Also read the text of the top N result pages (0 = snippets only).")
    max_page_chars: int = Field(default=4000, ge=200, le=50_000)
    region: str = Field(default="wt-wt", description="DuckDuckGo region, e.g. us-en, in-en, de-de; wt-wt = no region.")
    safe_search: Literal["on", "moderate", "off"] = "moderate"
    time_range: Literal["any", "day", "week", "month", "year"] = "any"
    fail_when_empty: bool = Field(default=True, description="Fail when nothing is found (so later nodes don't read an empty list).")
    timeout_seconds: float = Field(default=20, gt=0, le=120, description="Per provider.")


class WebSearchResult(BaseModel):
    results: list[dict[str, Any]]
    count: int
    query: str
    provider: str
    provider_used: str
    fallback_errors: list[dict[str, str]]


async def read_page(context: NodeContext, url: str, max_chars: int) -> dict[str, Any]:
    """{page_title, page_text} for a result, or {page_error} (never raises)."""
    config = WebPageConfig(url=url, max_chars=max_chars)
    try:
        fetched = await fetch_url(
            context.services, url, timeout=config.timeout_seconds, accept="text/html, application/xhtml+xml, text/plain;q=0.8"
        )
        if fetched.content_type == "text/plain":
            text, title = fetched.text.strip(), None
        elif not fetched.content_type or "html" in fetched.content_type or "xml" in fetched.content_type:
            data = await asyncio.to_thread(extract_page, fetched.text, fetched.url, config)
            text, title = data["text"], data["title"]
        else:
            return {"page_error": f"{fetched.content_type} isn't a web page"}
    except FetchError as exc:
        return {"page_error": str(exc)}
    text, _ = clip(text, max_chars)
    return {"page_title": title, "page_text": text} if text else {"page_error": "no readable text"}


@register_node("web_search")
class WebSearchNode(NodeDefinition[WebSearchConfig]):
    category = "sources"
    label = "Web Search"
    description = "Searches the web (DuckDuckGo, or Tavily with a key) and returns titles, URLs, and snippets."
    icon = "search"
    config_schema = WebSearchConfig
    output_schema = WebSearchResult

    def required_providers(self, node: GraphNode) -> list[tuple[str, str]]:
        provider = node.config.get("provider", "duckduckgo")
        return [("tavily", "provider")] if provider == "tavily" and not contains_reference(provider) else []

    async def execute(self, context: NodeContext, config: WebSearchConfig) -> NodeResult:
        query = " ".join(config.query.split())
        chain = [config.provider]
        if config.fallback:
            other = OTHER[config.provider]
            if other == "duckduckgo" or context.services.has_credentials(other):
                chain.append(other)
        errors: list[dict[str, str]] = []
        results: list[dict[str, Any]] | None = None
        used = config.provider
        for name in chain:
            try:
                provider = context.services.search(name)
                found = await asyncio.wait_for(
                    provider.search(
                        query, max_results=config.max_results, region=config.region, safe_search=config.safe_search,
                        time_range=None if config.time_range == "any" else config.time_range,
                    ),
                    timeout=config.timeout_seconds,
                )
            except ProviderError as exc:
                errors.append({"provider": name, "error": str(exc)})
                logger.warning("search provider failed", extra={"node_id": context.node_id, "provider": name, "error": str(exc)})
                continue
            except TimeoutError:
                errors.append({"provider": name, "error": f"{name}: no results within {config.timeout_seconds:g}s"})
                continue
            if not found and name != chain[-1]:
                errors.append({"provider": name, "error": f"{name}: no results"})
                continue
            results, used = found, name
            break
        if results is None:
            detail = "; ".join(e["error"] for e in errors)
            # DuckDuckGo alone had no fallback: say how to get one.
            no_fallback = chain == ["duckduckgo"] and "TAVILY_API_KEY" not in detail
            hint = " Set TAVILY_API_KEY so searches can fall back to Tavily." if no_fallback else ""
            return NodeResult.fail(f"Web search failed: {detail}.{hint}", fallback_errors=errors)

        seen: set[str] = set()
        unique = []
        for result in results:
            if result["url"] not in seen:
                seen.add(result["url"])
                unique.append({**result, "position": len(unique) + 1, "source": used})
        unique = unique[: config.max_results]
        if config.fetch_pages:
            pages = await asyncio.gather(*(read_page(context, r["url"], config.max_page_chars) for r in unique[: config.fetch_pages]))
            for result, page in zip(unique, pages, strict=False):
                result.update(page)
        if not unique and config.fail_when_empty:
            return NodeResult.fail(f"No results for '{query}'", provider_used=used, fallback_errors=errors)
        return NodeResult.ok(
            results=unique, count=len(unique), query=query, provider=config.provider, provider_used=used, fallback_errors=errors
        )
