"""Free data sources: RSS/Atom feeds (feedparser) and readable web page text (trafilatura).

Both download through flowforge_engine.fetch, so the SSRF guard applies to the URL and to
every redirect. Parsing and extraction are CPU work and run in a thread.
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from flowforge_engine.fetch import FetchError, fetch_url
from flowforge_engine.models import NodeContext, NodeResult
from flowforge_engine.registry import NodeConfig, NodeDefinition, register_node
from flowforge_engine.textutil import clip, html_to_text

# How many entry ids an RSS node remembers for "since last run".
MAX_SEEN_IDS = 1000


# --- RSS / Atom -----------------------------------------------------------------------------


class RSSConfig(NodeConfig):
    url: str = Field(min_length=1, description="The feed's URL (RSS 2.0, RSS 1.0, or Atom).")
    max_items: int = Field(default=10, ge=1, le=100, description="Newest N entries.")
    since_last_run: bool = Field(
        default=False,
        description=(
            "Only entries this node hasn't returned in an earlier successful run of the workflow. "
            "The first run returns the newest max_items; entries beyond max_items are skipped, not saved for later."
        ),
    )
    include_content: bool = Field(default=False, description="Also return each entry's full content (can be long).")
    max_summary_chars: int = Field(default=1000, ge=50, le=20_000)
    timeout_seconds: float = Field(default=15, gt=0, le=60)


class FeedInfo(BaseModel):
    title: str | None
    link: str | None
    description: str | None


class RSSResult(BaseModel):
    items: list[dict[str, Any]]
    count: int
    feed: FeedInfo
    total_entries: int
    new_entries: int
    first_run: bool


def _iso(parsed: Any) -> str | None:
    if not parsed:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(parsed), UTC).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def entry_id(entry: dict[str, Any]) -> str:
    """A stable id: the entry's guid/id, else its link, else a hash of title + date."""
    for key in ("id", "guid", "link"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    basis = f"{entry.get('title', '')}|{entry.get('published', '')}|{entry.get('updated', '')}"
    return "sha1:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()


def parse_feed(content: bytes, config: RSSConfig) -> dict[str, Any]:
    """Entries (newest first) and feed metadata. Raises ValueError for something that isn't a feed."""
    import feedparser

    parsed = feedparser.parse(content)
    # `version` ("rss20", "atom10", ...) is empty when no feed format was recognized.
    if not parsed.entries and not parsed.get("version"):
        reason = getattr(parsed, "bozo_exception", None)
        raise ValueError(f"not an RSS or Atom feed{f' ({reason})' if reason else ''}")
    items = []
    for entry in parsed.entries:
        summary, _ = clip(html_to_text(entry.get("summary", "") or ""), config.max_summary_chars)
        item = {
            "id": entry_id(entry),
            "title": html_to_text(entry.get("title", "") or ""),
            "link": entry.get("link"),
            "summary": summary,
            "published": _iso(entry.get("published_parsed") or entry.get("updated_parsed")),
            "author": entry.get("author"),
            "tags": [t.get("term") for t in entry.get("tags", []) if t.get("term")],
        }
        if config.include_content:
            item["content"] = "\n\n".join(html_to_text(c.get("value", "")) for c in entry.get("content", []))
        items.append(item)
    # Newest first when dates are known; undated entries keep feed order after them.
    dated = sorted((i for i in items if i["published"]), key=lambda i: i["published"], reverse=True)
    undated = [i for i in items if not i["published"]]
    feed = parsed.feed
    return {
        "items": dated + undated,
        "feed": {
            "title": feed.get("title"),
            "link": feed.get("link"),
            "description": html_to_text(feed.get("subtitle", "") or feed.get("description", "") or "") or None,
        },
    }


@register_node("rss")
class RSSNode(NodeDefinition[RSSConfig]):
    category = "sources"
    label = "RSS Feed"
    description = "Reads an RSS or Atom feed: newest entries, optionally only the ones since the last run."
    icon = "rss"
    config_schema = RSSConfig
    output_schema = RSSResult

    async def execute(self, context: NodeContext, config: RSSConfig) -> NodeResult:
        try:
            fetched = await fetch_url(
                context.services, config.url, timeout=config.timeout_seconds,
                accept="application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5",
            )
            data = await asyncio.to_thread(parse_feed, fetched.content, config)
        except (FetchError, ValueError) as exc:
            return NodeResult.fail(f"RSS feed {config.url}: {exc}")

        entries = data["items"]
        node_id = context.node_id or "rss"
        previous = await context.services.state.load(node_id) if config.since_last_run else None
        seen = set((previous or {}).get("seen", []))
        fresh = [e for e in entries if e["id"] not in seen] if previous is not None else entries
        items = fresh[: config.max_items]
        if config.since_last_run:
            # Everything in the feed now counts as seen; keep the newest ids first.
            current = [e["id"] for e in entries]
            in_feed = set(current)
            remembered = current + [i for i in (previous or {}).get("seen", []) if i not in in_feed]
            await context.services.state.save(node_id, {
                "seen": remembered[:MAX_SEEN_IDS], "url": config.url, "saved_at": datetime.now(UTC).isoformat(),
            })
        return NodeResult.ok(
            items=items,
            count=len(items),
            feed=data["feed"],
            total_entries=len(entries),
            new_entries=len(fresh),
            first_run=config.since_last_run and previous is None,
        )


# --- Web page --------------------------------------------------------------------------------


class WebPageConfig(NodeConfig):
    url: str = Field(min_length=1, description="The page to read (http:// or https://).")
    max_chars: int = Field(default=20_000, ge=100, le=500_000, description="Longer text is cut (reported as truncated).")
    include_links: bool = Field(default=False, description="Keep link targets in the text.")
    include_tables: bool = True
    timeout_seconds: float = Field(default=15, gt=0, le=60)
    fail_on_error: bool = Field(
        default=True,
        description="Off: a page that can't be read (blocked, 403, not HTML) gives empty text and `error` instead of failing the run.",
    )


class WebPageResult(BaseModel):
    text: str
    title: str | None
    author: str | None
    date: str | None
    description: str | None
    site_name: str | None
    url: str
    status_code: int
    content_type: str
    char_count: int
    truncated: bool
    extractor: str
    error: str | None = None


def extract_page(markup: str, url: str, config: WebPageConfig) -> dict[str, Any]:
    """The page's main text and metadata (trafilatura), or all visible text when it finds no
    main content (a very short or unusual page)."""
    import trafilatura

    document = trafilatura.bare_extraction(
        markup, url=url, with_metadata=True, include_comments=False,
        include_tables=config.include_tables, include_links=config.include_links,
    )
    meta = document.as_dict() if document is not None else {}
    text = (meta.get("text") or "").strip()
    extractor = "trafilatura"
    if not text:
        text, extractor = html_to_text(markup), "html"
    return {
        "text": text,
        "title": meta.get("title"),
        "author": meta.get("author"),
        "date": meta.get("date"),
        "description": meta.get("description"),
        "site_name": meta.get("sitename") or meta.get("hostname"),
        "extractor": extractor,
    }


@register_node("web_page")
class WebPageNode(NodeDefinition[WebPageConfig]):
    category = "sources"
    label = "Web Page"
    description = "Fetches a web page and extracts its readable text and metadata (trafilatura)."
    icon = "newspaper"
    config_schema = WebPageConfig
    output_schema = WebPageResult

    async def execute(self, context: NodeContext, config: WebPageConfig) -> NodeResult:
        result = await self._read(context, config)
        if result.success or config.fail_on_error:
            return result
        return NodeResult.ok(
            text="", title=None, author=None, date=None, description=None, site_name=None, url=config.url,
            status_code=0, content_type="unknown", char_count=0, truncated=False, extractor="none", error=result.error,
        )

    async def _read(self, context: NodeContext, config: WebPageConfig) -> NodeResult:
        try:
            fetched = await fetch_url(
                context.services, config.url, timeout=config.timeout_seconds,
                accept="text/html, application/xhtml+xml, text/plain;q=0.8, */*;q=0.5",
            )
        except FetchError as exc:
            return NodeResult.fail(str(exc))
        kind = fetched.content_type
        if kind == "text/plain":
            data = {"text": fetched.text.strip(), "title": None, "author": None, "date": None,
                    "description": None, "site_name": None, "extractor": "plain"}
        elif not kind or "html" in kind or "xml" in kind:
            data = await asyncio.to_thread(extract_page, fetched.text, fetched.url, config)
        else:
            return NodeResult.fail(
                f"{fetched.url} is {kind}, not a web page (for PDFs, upload the file and use PDF Extract)"
            )
        text, truncated = clip(data.pop("text"), config.max_chars)
        if not text:
            return NodeResult.fail(f"No readable text found at {fetched.url}")
        return NodeResult.ok(
            text=text,
            **data,
            url=fetched.url,
            status_code=fetched.status_code,
            content_type=kind or "unknown",
            char_count=len(text),
            truncated=truncated or fetched.truncated,
        )
