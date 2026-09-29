"""Notion and Airtable: request building, pagination, filters, error handling (tokens never
leak), the nodes, and auth-missing validation. HTTP goes to httpx.MockTransport."""

import json

import httpx
import pytest

from flowforge_engine import (
    ExecutionServices,
    IssueCode,
    NodeStatus,
    ProviderSettings,
    WorkflowGraph,
    execute_node,
    validate_workflow,
)
from flowforge_engine.errors import ProviderError
from flowforge_engine.providers import RetryPolicy
from flowforge_engine.providers.workspace import (
    NOTION_VERSION,
    AirtableClient,
    NotionClient,
    notion_filter,
    notion_id,
    simplify_property,
    text_to_blocks,
)
from flowforge_engine.testing import make_context, node

NOTION_TOKEN = "ntn_FakeTokenForTests0123456789abcdefABCDEF"
AIRTABLE_TOKEN = "patFakeTokenForTests.0123456789abcdef0123456789abcdef"
DB = "1f2e3d4c5b6a47988776655443322110"
DB_DASHED = "1f2e3d4c-5b6a-4798-8776-655443322110"
SOURCE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class Api:
    """A scripted HTTP server: each handler(request) -> Response, in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        return response(request) if callable(response) else response

    async def sleep(self, seconds):
        self.sleeps.append(seconds)

    def notion(self):
        return NotionClient(NOTION_TOKEN, transport=httpx.MockTransport(self.handler), sleep=self.sleep,
                            retry=RetryPolicy(max_retries=2, base_delay=0.01, max_delay=30))

    def airtable(self):
        return AirtableClient(AIRTABLE_TOKEN, transport=httpx.MockTransport(self.handler), sleep=self.sleep,
                              retry=RetryPolicy(max_retries=2, base_delay=0.01, max_delay=30))


def body(request: httpx.Request):
    return json.loads(request.content) if request.content else None


DATABASE = httpx.Response(200, json={"object": "database", "id": DB_DASHED, "data_sources": [{"id": SOURCE, "name": "Tasks"}]})
SCHEMA = httpx.Response(200, json={"object": "data_source", "id": SOURCE, "properties": {
    "Task": {"id": "title", "type": "title", "title": {}},
    "Status": {"id": "s", "type": "status", "status": {}},
    "Notes": {"id": "n", "type": "rich_text", "rich_text": {}},
    "Points": {"id": "p", "type": "number", "number": {}},
    "Done": {"id": "d", "type": "checkbox", "checkbox": {}},
    "Tags": {"id": "t", "type": "multi_select", "multi_select": {}},
    "Due": {"id": "u", "type": "date", "date": {}},
}})


# --- Notion helpers ----------------------------------------------------------------------------


def test_notion_ids_from_ids_and_urls():
    assert notion_id(DB) == DB_DASHED
    assert notion_id(DB_DASHED.upper()) == DB_DASHED
    assert notion_id(f"https://www.notion.so/acme/Tasks-{DB}?v=0123456789abcdef0123456789abcdef") == DB_DASHED
    with pytest.raises(ValueError, match="isn't a Notion id"):
        notion_id("tasks")


def test_text_becomes_simple_blocks():
    blocks = text_to_blocks("# Plan\nIntro line\n\n- one\n* two\n1. first\n- [ ] todo\n- [x] done\n> quoted\n### small")
    assert [b["type"] for b in blocks] == [
        "heading_1", "paragraph", "bulleted_list_item", "bulleted_list_item", "numbered_list_item", "to_do", "to_do",
        "quote", "heading_3"]
    assert blocks[0]["heading_1"]["rich_text"][0]["text"]["content"] == "Plan"
    assert blocks[5]["to_do"]["checked"] is False and blocks[6]["to_do"]["checked"] is True
    # Notion's 2,000-character limit per text object.
    long = text_to_blocks("x" * 4500)[0]["paragraph"]["rich_text"]
    assert [len(part["text"]["content"]) for part in long] == [2000, 2000, 500]


@pytest.mark.parametrize(
    ("prop_type", "operator", "value", "expected"),
    [
        ("title", "contains", "fix", {"title": {"contains": "fix"}}),
        ("rich_text", "equals", 5, {"rich_text": {"equals": "5"}}),
        ("status", "equals", "Done", {"status": {"equals": "Done"}}),
        ("select", "equals", "High", {"select": {"equals": "High"}}),
        ("multi_select", "equals", "bug", {"multi_select": {"contains": "bug"}}),
        ("number", "equals", "3", {"number": {"equals": 3}}),
        ("number", "equals", 2.5, {"number": {"equals": 2.5}}),
        ("checkbox", "equals", "yes", {"checkbox": {"equals": True}}),
        ("checkbox", "equals", False, {"checkbox": {"equals": False}}),
    ],
)
def test_filters_follow_the_property_type(prop_type, operator, value, expected):
    assert notion_filter("P", prop_type, operator, value) == {"property": "P", **expected}


@pytest.mark.parametrize(
    ("prop_type", "operator", "value", "message"),
    [
        ("status", "contains", "x", "use 'equals'"),
        ("number", "equals", "many", "isn't a number"),
        ("checkbox", "equals", "maybe", "use true or false"),
        ("date", "equals", "2026-01-01", "filters here support"),
    ],
)
def test_unsupported_filters_are_explained(prop_type, operator, value, message):
    with pytest.raises(ValueError, match=message):
        notion_filter("P", prop_type, operator, value)


def test_property_values_are_simplified():
    assert simplify_property({"type": "title", "title": [{"plain_text": "Ship "}, {"plain_text": "it"}]}) == "Ship it"
    assert simplify_property({"type": "status", "status": {"name": "Done"}}) == "Done"
    assert simplify_property({"type": "multi_select", "multi_select": [{"name": "a"}, {"name": "b"}]}) == ["a", "b"]
    assert simplify_property({"type": "number", "number": 3}) == 3
    assert simplify_property({"type": "date", "date": {"start": "2026-10-06", "end": None}}) == {"start": "2026-10-06", "end": None}
    assert simplify_property({"type": "formula", "formula": {"type": "string", "string": "ok"}}) == "ok"


# --- Notion client -----------------------------------------------------------------------------


async def test_create_page_uses_the_data_source_and_its_title_property():
    api = Api(DATABASE, SCHEMA, httpx.Response(200, json={"object": "page", "id": "page-1", "url": "https://www.notion.so/page-1"}))
    result = await api.notion().create_page(f"https://www.notion.so/Tasks-{DB}", "Ship v2.1", "# Notes\n- docs by Friday")
    database, schema, create = api.requests
    assert (database.method, database.url.path) == ("GET", f"/v1/databases/{DB_DASHED}")
    assert (schema.method, schema.url.path) == ("GET", f"/v1/data_sources/{SOURCE}")
    assert (create.method, create.url.path) == ("POST", "/v1/pages")
    for request in api.requests:
        assert request.headers["authorization"] == f"Bearer {NOTION_TOKEN}"
        assert request.headers["notion-version"] == NOTION_VERSION
    sent = body(create)
    assert sent["parent"] == {"type": "data_source_id", "data_source_id": SOURCE}
    assert sent["properties"] == {"Task": {"title": [{"type": "text", "text": {"content": "Ship v2.1"}}]}}
    assert [b["type"] for b in sent["children"]] == ["heading_1", "bulleted_list_item"]
    assert result == {"page_id": "page-1", "url": "https://www.notion.so/page-1", "blocks": 2, "title_property": "Task"}


async def test_long_bodies_are_appended_in_batches_of_100():
    api = Api(DATABASE, SCHEMA, httpx.Response(200, json={"id": "page-1", "url": "u"}),
              httpx.Response(200, json={}), httpx.Response(200, json={}))
    await api.notion().create_page(DB, "Long", "\n".join(f"line {i}" for i in range(250)))
    create, first, second = api.requests[2:]
    assert len(body(create)["children"]) == 100
    assert (first.method, first.url.path) == ("PATCH", "/v1/blocks/page-1/children") and len(body(first)["children"]) == 100
    assert len(body(second)["children"]) == 50


async def test_query_builds_the_filter_and_follows_cursors():
    def page(n):
        return {"object": "page", "id": f"p{n}", "url": f"https://www.notion.so/p{n}", "created_time": "t", "last_edited_time": "t",
                "properties": {"Task": {"type": "title", "title": [{"plain_text": f"Task {n}"}]},
                               "Status": {"type": "status", "status": {"name": "Open"}}}}

    api = Api(DATABASE, SCHEMA,
              httpx.Response(200, json={"results": [page(1), page(2)], "has_more": True, "next_cursor": "c2"}),
              httpx.Response(200, json={"results": [page(3)], "has_more": False, "next_cursor": None}))
    result = await api.notion().query(DB, prop="Status", operator="equals", value="Open", max_results=5)
    first, second = api.requests[2:]
    assert first.url.path == f"/v1/data_sources/{SOURCE}/query"
    assert body(first) == {"filter": {"property": "Status", "status": {"equals": "Open"}}, "page_size": 5}
    assert body(second) == {"filter": {"property": "Status", "status": {"equals": "Open"}}, "page_size": 3, "start_cursor": "c2"}
    assert [p["id"] for p in result["pages"]] == ["p1", "p2", "p3"]
    assert result["pages"][0]["properties"] == {"Task": "Task 1", "Status": "Open"}


async def test_query_with_an_unknown_property_names_the_real_ones():
    api = Api(DATABASE, SCHEMA)
    with pytest.raises(ProviderError, match="no property 'Priority'.*Done, Due, Notes"):
        await api.notion().query(DB, prop="Priority", value="High")


async def test_notion_errors_are_clear_and_hide_the_token():
    unshared = Api(httpx.Response(404, json={"object": "error", "code": "object_not_found",
                                             "message": f"Could not find database. token {NOTION_TOKEN}"}))
    with pytest.raises(ProviderError) as caught:
        await unshared.notion().create_page(DB, "x")
    assert "Connections" in str(caught.value) and NOTION_TOKEN not in str(caught.value)
    bad = Api(httpx.Response(401, json={"code": "unauthorized", "message": "API token is invalid."}))
    with pytest.raises(ProviderError, match="integration token was rejected"):
        await bad.notion().verify()


async def test_notion_429_waits_for_retry_after():
    api = Api(httpx.Response(429, headers={"retry-after": "2"}, json={"code": "rate_limited", "message": "slow down"}),
              httpx.Response(200, json={"object": "user", "name": "FlowForge", "bot": {"workspace_name": "Acme"}}))
    assert await api.notion().verify() == {"bot": "FlowForge", "workspace": "Acme"}
    assert len(api.sleeps) == 1 and 2.0 <= api.sleeps[0] < 3  # Retry-After, plus jitter
    assert api.requests[-1].url.path == "/v1/users/me"


# --- Airtable client ---------------------------------------------------------------------------


async def test_airtable_create_record():
    api = Api(httpx.Response(200, json={"id": "rec1", "createdTime": "2026-09-29T10:00:00.000Z", "fields": {"Name": "Ada"}}))
    record = await api.airtable().create_record("appABC123", "Leads & Deals", {"Name": "Ada", "Score": 7}, typecast=True)
    request = api.requests[0]
    assert (request.method, request.url.raw_path.decode()) == ("POST", "/v0/appABC123/Leads%20%26%20Deals")
    assert request.headers["authorization"] == f"Bearer {AIRTABLE_TOKEN}"
    assert body(request) == {"fields": {"Name": "Ada", "Score": 7}, "typecast": True}
    assert record["id"] == "rec1"


async def test_airtable_list_paginates_with_formula_and_limit():
    api = Api(httpx.Response(200, json={"records": [{"id": f"r{i}"} for i in range(100)], "offset": "o2"}),
              httpx.Response(200, json={"records": [{"id": f"s{i}"} for i in range(100)], "offset": "o3"}))
    records = await api.airtable().list_records("appABC123", "Leads", formula="{Status} = 'Open'", max_records=150, view="Grid")
    first, second = api.requests
    assert dict(first.url.params) == {"pageSize": "100", "maxRecords": "150", "filterByFormula": "{Status} = 'Open'", "view": "Grid"}
    assert dict(second.url.params)["offset"] == "o2" and dict(second.url.params)["pageSize"] == "50"
    assert len(records) == 150


@pytest.mark.parametrize(
    ("status", "payload", "message"),
    [
        (401, {"error": {"type": "AUTHENTICATION_REQUIRED", "message": "Authentication required"}}, "token was rejected"),
        (403, {"error": {"type": "INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND", "message": "Invalid permissions"}}, "data.records:read"),
        (404, {"error": "NOT_FOUND"}, "base or table not found"),
        (422, {"error": {"type": "UNKNOWN_FIELD_NAME", "message": 'Unknown field name: "Nmae"'}}, 'Unknown field name: "Nmae"'),
    ],
)
async def test_airtable_errors_are_clear(status, payload, message):
    api = Api(httpx.Response(status, json=payload))
    with pytest.raises(ProviderError, match=message) as caught:
        await api.airtable().create_record("appABC123", "Leads", {"Nmae": "x"})
    assert caught.value.status_code == status and AIRTABLE_TOKEN not in str(caught.value)


async def test_airtable_5xx_is_retried_then_verify_works():
    api = Api(httpx.Response(503, text="unavailable"), httpx.Response(200, json={"id": "usr1", "scopes": ["data.records:read"]}))
    assert await api.airtable().verify() == {"user_id": "usr1", "scopes": ["data.records:read"], "email": None}
    assert api.requests[-1].url.path == "/v0/meta/whoami" and len(api.sleeps) == 1


# --- the nodes -----------------------------------------------------------------------------------


class Recorder:
    """A workspace client that records calls (injected into ExecutionServices)."""

    is_mock = False

    def __init__(self, fail: ProviderError | None = None):
        self.calls: list[tuple] = []
        self.fail = fail

    async def create_page(self, database_id, title, content=""):
        self.calls.append(("create_page", database_id, title, content))
        if self.fail:
            raise self.fail
        return {"page_id": "p1", "url": "https://www.notion.so/p1", "blocks": 1, "title_property": "Task"}

    async def query(self, database_id, **kwargs):
        self.calls.append(("query", database_id, kwargs))
        return {"pages": [{"id": "p1"}], "data_source_id": SOURCE}

    async def create_record(self, base_id, table, fields, typecast=False):
        self.calls.append(("create_record", base_id, table, fields, typecast))
        return {"id": "rec1", "createdTime": "t", "fields": fields}

    async def list_records(self, base_id, table, **kwargs):
        self.calls.append(("list_records", base_id, table, kwargs))
        return [{"id": "rec1", "createdTime": "t", "fields": {"Name": "Ada"}}]


def services(**clients):
    return ExecutionServices(provider_settings=ProviderSettings(testing=True), workspace_clients=clients)


async def test_notion_nodes():
    notion = Recorder()
    created = await execute_node(
        node("n", "notion_create_page", database_id=DB, title="Ship", content="hello"), make_context(services=services(notion=notion)))
    assert created.status is NodeStatus.SUCCESS and created.output["page_id"] == "p1" and created.output["mock"] is False
    queried = await execute_node(
        node("q", "notion_query_database", database_id=DB, filter_property=" Status ", filter_value="Open"),
        make_context(services=services(notion=notion)))
    assert queried.status is NodeStatus.SUCCESS and queried.output["count"] == 1
    assert notion.calls[1] == ("query", DB, {"prop": "Status", "operator": "equals", "value": "Open", "max_results": 100})
    failing = Recorder(fail=ProviderError("notion", "not shared with the integration"))
    result = await execute_node(node("n", "notion_create_page", database_id=DB, title="x"), make_context(services=services(notion=failing)))
    assert result.status is NodeStatus.FAILED and "not shared" in result.error


async def test_airtable_nodes():
    airtable = Recorder()
    created = await execute_node(
        node("a", "airtable_create_record", base_id="appABC123", table_name="Leads", fields={"Name": "Ada"}),
        make_context(services=services(airtable=airtable)))
    assert created.status is NodeStatus.SUCCESS and created.output["record_id"] == "rec1"
    listed = await execute_node(
        node("l", "airtable_list_records", base_id="appABC123", table_name="Leads", filter_formula="  ", max_records=10),
        make_context(services=services(airtable=airtable)))
    assert listed.output == {"records": [{"id": "rec1", "created_time": "t", "fields": {"Name": "Ada"}}], "count": 1, "mock": False}
    assert airtable.calls[1] == ("list_records", "appABC123", "Leads", {"formula": None, "max_records": 10, "view": None})
    empty = await execute_node(node("a", "airtable_create_record", base_id="appABC123", table_name="Leads", fields={}),
                               make_context(services=services(airtable=airtable)))
    assert empty.status is NodeStatus.FAILED and "fields is empty" in empty.error


def graph(*nodes, edges=()):
    return WorkflowGraph.model_validate({"nodes": list(nodes), "edges": [{"source": a, "target": b} for a, b in edges]})


def test_config_validation():
    bad = graph(
        {"id": "n", "type": "notion_create_page", "config": {"database_id": "tasks", "title": "x"}},
        {"id": "a", "type": "airtable_list_records", "config": {"base_id": "tblABC", "table_name": "Leads"}},
        {"id": "input", "type": "input", "config": {"name": "db", "input_type": "text"}},
        # A reference is checked at run time, not by the id rule.
        {"id": "r", "type": "notion_query_database", "config": {"database_id": "{{input.value}}"}},
        edges=[("input", "r")],
    )
    issues = validate_workflow(bad, services=services(notion=Recorder(), airtable=Recorder()))
    messages = {i.node_id: i.message for i in issues}
    assert set(messages) == {"n", "a"}
    assert "isn't a Notion id" in messages["n"] and "starts with 'app'" in messages["a"]


def test_auth_missing_is_a_validation_error():
    workflow = graph(
        {"id": "n", "type": "notion_query_database", "config": {"database_id": DB}},
        {"id": "a", "type": "airtable_create_record", "config": {"base_id": "appABC123", "table_name": "T", "fields": {"x": 1}}},
    )
    issues = validate_workflow(workflow, services=ExecutionServices(provider_settings=ProviderSettings()))
    assert [(i.node_id, i.code, i.field) for i in issues] == [
        ("n", IssueCode.AUTH_MISSING, "database_id"), ("a", IssueCode.AUTH_MISSING, "base_id")]
    assert "NOTION_API_KEY" in issues[0].message and "AIRTABLE_API_KEY" in issues[1].message
    configured = ProviderSettings(notion={"api_key": "ntn_x"}, airtable={"api_key": "pat_x"})
    assert validate_workflow(workflow, services=ExecutionServices(provider_settings=configured)) == []
