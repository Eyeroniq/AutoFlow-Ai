"""RSS/Atom and Web Page nodes against an in-process HTTP transport: parsing, newest-first
order, "since last run" state, extraction, content types, and the SSRF guard."""

import httpx
import pytest

from flowforge_engine import ExecutionServices, MemoryStateStore, NodeStatus, ProviderSettings, ReadOnlyStateStore, execute_node
from flowforge_engine.testing import make_context, node

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example News</title><link>https://news.example.com/</link><description>All the &lt;b&gt;news&lt;/b&gt;</description>
  {items}
</channel></rss>"""

ITEM = """<item><title>{title}</title><link>https://news.example.com/{slug}</link><guid>{slug}</guid>
  <pubDate>{date}</pubDate><description>&lt;p&gt;{summary} &amp;amp; more&lt;/p&gt;</description>
  <category>tech</category></item>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Blog</title><link href="https://blog.example.org/"/>
  <entry><title>Hello Atom</title><link href="https://blog.example.org/hello"/><id>tag:blog,1</id>
    <updated>2026-09-27T10:00:00Z</updated><summary>First post</summary><author><name>Ann</name></author>
    <content type="html">&lt;p&gt;Full &lt;em&gt;content&lt;/em&gt;&lt;/p&gt;</content></entry>
</feed>"""


def rss(*entries):
    return RSS.format(items="".join(ITEM.format(**e) for e in entries))


ONE = {"title": "Older story", "slug": "older", "date": "Sat, 26 Sep 2026 08:00:00 GMT", "summary": "Old"}
TWO = {"title": "Newer story", "slug": "newer", "date": "Sun, 27 Sep 2026 09:30:00 GMT", "summary": "New"}
THREE = {"title": "Newest story", "slug": "newest", "date": "Mon, 28 Sep 2026 06:00:00 GMT", "summary": "Fresh"}


class Site:
    """Serves {url: (status, content type, body)} and records the requests."""

    def __init__(self, pages):
        self.pages = pages
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, content_type, body = self.pages.get(str(request.url), (404, "text/plain", "not found"))
        return httpx.Response(status, headers={"content-type": content_type}, text=body)

    def services(self, state=None):
        return ExecutionServices(
            provider_settings=ProviderSettings(testing=True),
            http_transport=httpx.MockTransport(self.handler),
            allow_private_network=False,
            state=state,
        )


FEED_URL = "https://news.example.com/rss.xml"


async def read_feed(services, **config):
    return await execute_node(node("news", "rss", url=FEED_URL, **config), make_context(services=services))


async def test_rss_items_are_parsed_newest_first():
    site = Site({FEED_URL: (200, "application/rss+xml", rss(ONE, THREE, TWO))})
    result = await read_feed(site.services(), max_items=2)
    assert result.status is NodeStatus.SUCCESS, result.error
    out = result.output
    assert [i["title"] for i in out["items"]] == ["Newest story", "Newer story"]
    newest = out["items"][0]
    assert newest["link"] == "https://news.example.com/newest" and newest["id"] == "newest"
    assert newest["published"] == "2026-09-28T06:00:00+00:00"
    assert newest["summary"] == "Fresh & more" and newest["tags"] == ["tech"]
    assert out["feed"] == {"title": "Example News", "link": "https://news.example.com/", "description": "All the news"}
    assert (out["count"], out["total_entries"]) == (2, 3)
    # A browser-like User-Agent and a feed Accept header.
    assert "FlowForgeBot" in site.requests[0].headers["user-agent"] and "rss" in site.requests[0].headers["accept"]


async def test_atom_feeds_and_full_content():
    site = Site({FEED_URL: (200, "application/atom+xml", ATOM)})
    result = await read_feed(site.services(), include_content=True)
    entry = result.output["items"][0]
    assert entry["title"] == "Hello Atom" and entry["author"] == "Ann" and entry["id"] == "tag:blog,1"
    assert entry["content"] == "Full content" and entry["published"] == "2026-09-27T10:00:00+00:00"


async def test_since_last_run_returns_only_new_entries():
    state = MemoryStateStore()
    site = Site({FEED_URL: (200, "application/rss+xml", rss(ONE, TWO))})
    first = await read_feed(site.services(state), since_last_run=True)
    assert [i["id"] for i in first.output["items"]] == ["newer", "older"] and first.output["first_run"] is True

    site.pages[FEED_URL] = (200, "application/rss+xml", rss(ONE, TWO, THREE))
    second = await read_feed(site.services(state), since_last_run=True)
    assert [i["id"] for i in second.output["items"]] == ["newest"]
    assert second.output["first_run"] is False and second.output["new_entries"] == 1

    third = await read_feed(site.services(state), since_last_run=True)
    assert third.output["items"] == [] and third.output["count"] == 0

    # Without the option, everything is returned and nothing is remembered.
    everything = await read_feed(site.services(MemoryStateStore()))
    assert everything.output["count"] == 3


async def test_a_test_run_does_not_move_the_since_last_run_position():
    state = MemoryStateStore()
    site = Site({FEED_URL: (200, "application/rss+xml", rss(ONE))})
    await read_feed(site.services(ReadOnlyStateStore(state)), since_last_run=True)
    assert await state.load("news") is None


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        ({FEED_URL: (404, "text/html", "gone")}, f"HTTP 404 from {FEED_URL}"),
        ({FEED_URL: (200, "text/html", "<html><body><p>Just a page</p></body></html>")}, "not an RSS or Atom feed"),
    ],
)
async def test_rss_errors(pages, message):
    result = await read_feed(Site(pages).services())
    assert result.status is NodeStatus.FAILED and message in result.error


@pytest.mark.parametrize("url", ["http://localhost:8000/feed", "http://10.0.0.7/rss", "http://redis/feed"])
async def test_feeds_and_pages_are_behind_the_ssrf_guard(url):
    site = Site({})
    for node_type in ("rss", "web_page"):
        result = await execute_node(node("n", node_type, url=url), make_context(services=site.services()))
        assert result.status is NodeStatus.FAILED and "Blocked request to" in result.error
    assert site.requests == []  # refused before any request


# --- Web Page --------------------------------------------------------------------------------

ARTICLE_URL = "https://blog.example.org/post"
ARTICLE = """<html><head><title>Ignore me | Blog</title><meta name="author" content="Ann Writer">
<meta name="description" content="Why queues matter"></head><body>
<nav><a href="/">Home</a> <a href="/about">About</a></nav>
<article><h1>Why queues matter</h1>
<p>Queues decouple producers from consumers, which lets each side scale on its own and absorb bursts of work.</p>
<p>They also make retries safe: a message that fails can be delivered again without the producer knowing about it.</p>
</article><footer>Copyright 2026 Example Blog. All rights reserved.</footer></body></html>"""


async def read_page(site, **config):
    return await execute_node(node("page", "web_page", url=ARTICLE_URL, **config), make_context(services=site.services()))


async def test_web_page_extracts_the_main_text_and_metadata():
    result = await read_page(Site({ARTICLE_URL: (200, "text/html; charset=utf-8", ARTICLE)}))
    assert result.status is NodeStatus.SUCCESS, result.error
    out = result.output
    assert "Queues decouple producers from consumers" in out["text"] and "make retries safe" in out["text"]
    assert "Home" not in out["text"] and "All rights reserved" not in out["text"]
    assert out["title"] == "Why queues matter" and out["author"] == "Ann Writer"
    assert out["description"] == "Why queues matter" and out["extractor"] == "trafilatura"
    assert out["status_code"] == 200 and out["content_type"] == "text/html" and out["truncated"] is False


async def test_web_page_truncates_plain_text_and_rejects_binaries():
    capped = await read_page(Site({ARTICLE_URL: (200, "text/plain", "word " * 100)}), max_chars=100)
    assert capped.output["extractor"] == "plain" and capped.output["truncated"] is True
    assert len(capped.output["text"]) == 100

    pdf = await read_page(Site({ARTICLE_URL: (200, "application/pdf", "%PDF-1.7")}))
    assert pdf.status is NodeStatus.FAILED and "is application/pdf, not a web page" in pdf.error

    empty = await read_page(Site({ARTICLE_URL: (200, "text/html", "<html><body></body></html>")}))
    assert empty.status is NodeStatus.FAILED and "No readable text" in empty.error
