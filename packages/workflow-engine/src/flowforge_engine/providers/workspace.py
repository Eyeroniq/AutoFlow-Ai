"""Notion and Airtable REST clients (httpx), with the shared retry helper.

Notion (https://developers.notion.com, Notion-Version 2026-03-11): an internal integration
token, sent as a Bearer token. Since 2025-09-03 a database holds one or more *data sources*;
rows are queried and created through the data source (POST /v1/data_sources/{id}/query,
parent {"type": "data_source_id"}). The nodes take the database id users copy from the URL
and use its first data source. Notion rate limits to ~3 requests/second and answers 429 with
Retry-After.

Airtable (https://airtable.com/developers/web/api): a personal access token as Bearer.
Records live at /v0/{base_id}/{table id or name}. Airtable allows 5 requests/second per base
and answers 429 when exceeded (wait ~30 s).

Tokens are redacted from every error.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

from flowforge_engine.errors import ProviderError
from flowforge_engine.providers.retry import RetryPolicy, parse_retry_after, redact, with_retries

NOTION_BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
AIRTABLE_BASE_URL = "https://api.airtable.com/v0"

# Notion limits: 2,000 characters per rich-text object, 100 blocks per request.
NOTION_TEXT_CHARS = 2000
NOTION_MAX_BLOCKS = 100

_UUID_HEX = re.compile(r"([0-9a-f]{32})(?:[?#/]|$)", re.IGNORECASE)
_UUID_DASHED = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def notion_id(value: str) -> str:
    """A Notion id from an id (with or without dashes) or a notion.so URL. Raises ValueError."""
    text = value.strip()
    if match := _UUID_DASHED.search(text):
        return match.group(0).lower()
    compact = text.replace("-", "")
    if match := _UUID_HEX.search(compact):
        raw = match.group(1).lower()
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    raise ValueError(f"'{value}' isn't a Notion id or page/database URL")


class _RestClient:
    name = "rest"
    is_mock = False
    base_url = ""

    def __init__(
        self,
        token: str,
        *,
        retry: RetryPolicy | None = None,
        timeout: float = 30,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._token = token
        self._retry = retry or RetryPolicy()
        self._timeout = timeout
        self._base = (base_url or self.base_url).rstrip("/")
        self._transport = transport
        self._sleep = sleep

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _message(self, status: int, data: Any, text: str) -> str:
        raise NotImplementedError

    def _refused(self, status: int, message: str) -> ProviderError:
        return ProviderError(self.name, f"request refused (HTTP {status}): {message}", status_code=status)

    async def _once(self, method: str, path: str, *, params: Any = None, json: Any = None) -> Any:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.request(method, f"{self._base}{path}", headers=self._headers(), params=params, json=json)
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, f"no answer within {self._timeout:g}s", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.name, f"network error: {type(exc).__name__}: {redact(str(exc), self._token)}", retryable=True
            ) from exc
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}
        status = response.status_code
        if status < 300:
            return data
        message = redact(self._message(status, data, response.text[:300]), self._token)
        if status == 429:
            raise ProviderError(
                self.name, f"rate limited (HTTP 429): {message}", status_code=status, retryable=True,
                retry_after=parse_retry_after(response.headers),
            )
        if status >= 500:
            raise ProviderError(self.name, f"server error (HTTP {status}): {message}", status_code=status, retryable=True)
        raise self._refused(status, message)

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        return await with_retries(lambda: self._once(method, path, **kwargs), self._retry, sleep=self._sleep)


# --- Notion ------------------------------------------------------------------------------


def _rich_text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": text[i:i + NOTION_TEXT_CHARS]}} for i in range(0, len(text), NOTION_TEXT_CHARS)] or []


_BLOCK_PREFIXES = (
    ("### ", "heading_3"), ("## ", "heading_2"), ("# ", "heading_1"),
    ("- [ ] ", "to_do"), ("- [x] ", "to_do_done"), ("- ", "bulleted_list_item"), ("* ", "bulleted_list_item"),
    ("> ", "quote"),
)
_NUMBERED = re.compile(r"^\d+[.)] ")


def text_to_blocks(content: str) -> list[dict[str, Any]]:
    """Plain text as Notion blocks: one block per line, with simple Markdown-style prefixes
    (#, ##, ###, "- ", "* ", "1. ", "- [ ] ", "- [x] ", "> "); other lines are paragraphs.
    Blank lines separate nothing (Notion spaces blocks itself)."""
    blocks: list[dict[str, Any]] = []
    for raw in content.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        kind, text = "paragraph", line
        for prefix, block_type in _BLOCK_PREFIXES:
            if line.startswith(prefix):
                kind, text = block_type, line[len(prefix):]
                break
        else:
            if _NUMBERED.match(line):
                kind, text = "numbered_list_item", _NUMBERED.sub("", line, count=1)
        if kind in ("to_do", "to_do_done"):
            blocks.append({"object": "block", "type": "to_do",
                           "to_do": {"rich_text": _rich_text(text), "checked": kind == "to_do_done"}})
        else:
            blocks.append({"object": "block", "type": kind, kind: {"rich_text": _rich_text(text)}})
    return blocks


# How "equals" / "contains" is written for each property type (None: not filterable here).
_TEXT_TYPES = {"title", "rich_text", "url", "email", "phone_number"}


def notion_filter(prop: str, prop_type: str, operator: str, value: Any) -> dict[str, Any]:
    """A property filter for a simple `equals` / `contains` on one property. Raises ValueError."""
    if prop_type in _TEXT_TYPES:
        return {"property": prop, prop_type: {operator: str(value)}}
    if prop_type in ("select", "status"):
        if operator != "equals":
            raise ValueError(f"'{prop}' is a {prop_type} property: use 'equals'")
        return {"property": prop, prop_type: {"equals": str(value)}}
    if prop_type == "multi_select":
        return {"property": prop, "multi_select": {"contains": str(value)}}
    if prop_type == "number":
        if operator != "equals":
            raise ValueError(f"'{prop}' is a number property: use 'equals'")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"'{prop}' is a number property; '{value}' isn't a number") from None
        return {"property": prop, "number": {"equals": int(number) if number.is_integer() else number}}
    if prop_type == "checkbox":
        if operator != "equals":
            raise ValueError(f"'{prop}' is a checkbox property: use 'equals'")
        truth = str(value).strip().lower()
        if truth not in ("true", "false", "yes", "no", "1", "0"):
            raise ValueError(f"'{prop}' is a checkbox; use true or false")
        return {"property": prop, "checkbox": {"equals": truth in ("true", "yes", "1")}}
    raise ValueError(f"'{prop}' is a {prop_type} property; filters here support text, select, status, multi-select, number, and checkbox")


def _plain(rich: list[dict[str, Any]] | None) -> str:
    return "".join(part.get("plain_text") or (part.get("text") or {}).get("content", "") for part in rich or [])


def simplify_property(prop: dict[str, Any]) -> Any:
    """A Notion property value as plain JSON (text, number, list, ...)."""
    kind = prop.get("type")
    value = prop.get(kind) if kind else None
    if kind in ("title", "rich_text"):
        return _plain(value)
    if kind in ("select", "status"):
        return (value or {}).get("name")
    if kind == "multi_select":
        return [option.get("name") for option in value or []]
    if kind == "date":
        return None if not value else {"start": value.get("start"), "end": value.get("end")}
    if kind == "people":
        return [person.get("name") or person.get("id") for person in value or []]
    if kind == "relation":
        return [rel.get("id") for rel in value or []]
    if kind == "files":
        return [f.get("name") for f in value or []]
    if kind == "formula":
        return (value or {}).get((value or {}).get("type", ""))
    if kind == "rollup":
        return (value or {}).get((value or {}).get("type", ""))
    if kind in ("created_by", "last_edited_by"):
        return (value or {}).get("name") or (value or {}).get("id")
    return value


class NotionClient(_RestClient):
    name = "notion"
    base_url = NOTION_BASE_URL

    def _headers(self) -> dict[str, str]:
        return {**super()._headers(), "Notion-Version": NOTION_VERSION}

    def _message(self, status: int, data: Any, text: str) -> str:
        if isinstance(data, dict) and data.get("message"):
            return f"{data.get('code', 'error')}: {data['message']}"
        return text or f"HTTP {status}"

    def _refused(self, status: int, message: str) -> ProviderError:
        if status == 401:
            hint = "the integration token was rejected; check it in Integrations (or NOTION_API_KEY)"
        elif status in (403, 404):
            hint = ("not found or not shared with the integration: open the page or database in Notion, "
                    "••• > Connections, and add your integration")
        else:
            hint = "Notion refused the request"
        return ProviderError(self.name, f"{hint} (HTTP {status}: {message})", status_code=status)

    async def data_source(self, database_id: str) -> dict[str, Any]:
        """The database's first data source, with its property schema."""
        database = await self._call("GET", f"/databases/{notion_id(database_id)}")
        sources = database.get("data_sources") or []
        if not sources:
            raise ProviderError(self.name, "the database has no data sources")
        return await self._call("GET", f"/data_sources/{sources[0]['id']}")

    async def create_page(self, database_id: str, title: str, content: str = "") -> dict[str, Any]:
        source = await self.data_source(database_id)
        title_prop = next((name for name, p in (source.get("properties") or {}).items() if p.get("type") == "title"), None)
        if title_prop is None:
            raise ProviderError(self.name, "the database has no title property")
        blocks = text_to_blocks(content)
        body: dict[str, Any] = {
            "parent": {"type": "data_source_id", "data_source_id": source["id"]},
            "properties": {title_prop: {"title": _rich_text(title)}},
        }
        if blocks:
            body["children"] = blocks[:NOTION_MAX_BLOCKS]
        page = await self._call("POST", "/pages", json=body)
        # Past 100 blocks, the rest are appended in batches of 100.
        for start in range(NOTION_MAX_BLOCKS, len(blocks), NOTION_MAX_BLOCKS):
            await self._call("PATCH", f"/blocks/{page['id']}/children", json={"children": blocks[start:start + NOTION_MAX_BLOCKS]})
        return {"page_id": page["id"], "url": page.get("url"), "blocks": len(blocks), "title_property": title_prop}

    async def query(
        self, database_id: str, *, prop: str | None = None, operator: str = "equals", value: Any = None, max_results: int = 100,
    ) -> dict[str, Any]:
        source = await self.data_source(database_id)
        body: dict[str, Any] = {}
        if prop:
            schema = (source.get("properties") or {}).get(prop)
            if schema is None:
                known = ", ".join(sorted(source.get("properties") or {}))
                raise ProviderError(self.name, f"the database has no property '{prop}' (it has: {known})")
            try:
                body["filter"] = notion_filter(prop, schema["type"], operator, value)
            except ValueError as exc:
                raise ProviderError(self.name, str(exc)) from None
        pages: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(pages) < max_results:
            request = {**body, "page_size": min(100, max_results - len(pages))}
            if cursor:
                request["start_cursor"] = cursor
            data = await self._call("POST", f"/data_sources/{source['id']}/query", json=request)
            for page in data.get("results", []):
                pages.append({
                    "id": page.get("id"), "url": page.get("url"),
                    "created_time": page.get("created_time"), "last_edited_time": page.get("last_edited_time"),
                    "properties": {name: simplify_property(p) for name, p in (page.get("properties") or {}).items()},
                })
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                break
        return {"pages": pages[:max_results], "data_source_id": source["id"]}

    async def verify(self) -> dict[str, Any]:
        """GET /users/me: the integration's bot user (proves the token; changes nothing)."""
        me = await self._call("GET", "/users/me")
        bot = me.get("bot") or {}
        return {"bot": me.get("name"), "workspace": bot.get("workspace_name")}


# --- Airtable ----------------------------------------------------------------------------


class AirtableClient(_RestClient):
    name = "airtable"
    base_url = AIRTABLE_BASE_URL

    def _message(self, status: int, data: Any, text: str) -> str:
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            return f"{error.get('type', 'error')}: {error.get('message', '')}".rstrip(": ")
        if isinstance(error, str):
            return error
        return text or f"HTTP {status}"

    def _refused(self, status: int, message: str) -> ProviderError:
        if status == 401:
            hint = "the personal access token was rejected; check it in Integrations (or AIRTABLE_API_KEY)"
        elif status in (403, 404):
            hint = ("base or table not found, or the token can't reach it: give the token the base under "
                    "Access and the scopes data.records:read / data.records:write")
        elif status == 422:
            hint = "Airtable refused the data (a field name or value doesn't match the table)"
        else:
            hint = "Airtable refused the request"
        return ProviderError(self.name, f"{hint} (HTTP {status}: {message})", status_code=status)

    @staticmethod
    def _path(base_id: str, table: str) -> str:
        return f"/{quote(base_id.strip(), safe='')}/{quote(table.strip(), safe='')}"

    async def create_record(self, base_id: str, table: str, fields: dict[str, Any], *, typecast: bool = False) -> dict[str, Any]:
        return await self._call("POST", self._path(base_id, table), json={"fields": fields, "typecast": typecast})

    async def list_records(
        self, base_id: str, table: str, *, formula: str | None = None, max_records: int = 100, view: str | None = None,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        offset: str | None = None
        while len(records) < max_records:
            params: dict[str, Any] = {"pageSize": min(100, max_records - len(records)), "maxRecords": max_records}
            if formula:
                params["filterByFormula"] = formula
            if view:
                params["view"] = view
            if offset:
                params["offset"] = offset
            data = await self._call("GET", self._path(base_id, table), params=params)
            records += data.get("records", [])
            offset = data.get("offset")
            if not offset:
                break
        return records[:max_records]

    async def verify(self) -> dict[str, Any]:
        """GET /meta/whoami: the token's user id and scopes (changes nothing)."""
        me = await self._call("GET", "/meta/whoami")
        return {"user_id": me.get("id"), "scopes": me.get("scopes"), "email": me.get("email")}


# --- mocks (the test suite, TESTING=true) --------------------------------------------------


class MockNotion:
    name = "notion"
    is_mock = True

    async def create_page(self, database_id: str, title: str, content: str = "") -> dict[str, Any]:
        return {"page_id": "mock-page", "url": "https://www.notion.so/mock-page", "blocks": len(text_to_blocks(content)),
                "title_property": "Name"}

    async def query(self, database_id: str, **_: Any) -> dict[str, Any]:
        return {"pages": [{"id": "mock-page", "url": "https://www.notion.so/mock-page", "properties": {"Name": "Mock"}}],
                "data_source_id": "mock-source"}

    async def verify(self) -> dict[str, Any]:
        return {"mock": True}


class MockAirtable:
    name = "airtable"
    is_mock = True

    async def create_record(self, base_id: str, table: str, fields: dict[str, Any], **_: Any) -> dict[str, Any]:
        return {"id": "recMOCK", "createdTime": "2026-01-01T00:00:00.000Z", "fields": fields}

    async def list_records(self, base_id: str, table: str, **_: Any) -> list[dict[str, Any]]:
        return [{"id": "recMOCK", "createdTime": "2026-01-01T00:00:00.000Z", "fields": {"Name": "Mock"}}]

    async def verify(self) -> dict[str, Any]:
        return {"mock": True}
