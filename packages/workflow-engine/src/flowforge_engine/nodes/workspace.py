"""Notion (Create Page, Query Database) and Airtable (Create Record, List Records).

Credentials: a Notion internal integration token (NOTION_API_KEY or a user's "notion"
credential) and an Airtable personal access token (AIRTABLE_API_KEY or "airtable"). The
REST clients are in flowforge_engine.providers.workspace. The create nodes aren't
interruptible, so a stop never leaves it unknown whether the page or record was created.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from flowforge_engine.errors import ProviderError
from flowforge_engine.models import GraphNode, NodeContext, NodeResult
from flowforge_engine.providers.workspace import notion_id
from flowforge_engine.registry import GuardedOutboundConfig, NodeConfig, NodeDefinition, register_node
from flowforge_engine.variables import contains_reference


def _check_notion_id(value: str) -> str:
    if not contains_reference(value):
        notion_id(value)  # raises ValueError with a clear message
    return value.strip()


def _check_base_id(value: str) -> str:
    value = value.strip()
    if not contains_reference(value) and not value.startswith("app"):
        raise ValueError("an Airtable base id starts with 'app' (it's in the base's URL: airtable.com/appXXXXXXXXXXXXXX/...)")
    return value


DATABASE_ID = "The database's id, or its URL (open it as a full page and copy the link). It must be shared with your integration."


class _Workspace:
    """Shared by the four nodes: the category and the credential they need."""

    category = "integration"
    provider = ""

    def required_providers(self, node: GraphNode) -> list[tuple[str, str]]:
        return [(self.provider, "database_id" if self.provider == "notion" else "base_id")]


# --- Notion --------------------------------------------------------------------------------


class NotionCreatePageConfig(GuardedOutboundConfig):
    database_id: str = Field(min_length=1, description=DATABASE_ID)
    title: str = Field(min_length=1, description="The page's title (the database's title property).")
    content: str = Field(
        default="",
        description="The page body, one block per line: plain lines are paragraphs; '# ', '## ', '### ' headings, "
        "'- ' bullets, '1. ' numbered items, '- [ ] ' / '- [x] ' to-dos, '> ' quotes.",
    )

    @field_validator("database_id")
    @classmethod
    def _database_id(cls, value: str) -> str:
        return _check_notion_id(value)


class NotionCreatePageResult(BaseModel):
    page_id: str
    url: str | None
    blocks: int
    title_property: str
    mock: bool


@register_node("notion_create_page")
class NotionCreatePageNode(_Workspace, NodeDefinition[NotionCreatePageConfig]):
    guard_fields = ('title', 'content')
    label = "Notion: Create Page"
    description = "Adds a page (a row) to a Notion database, with a title and a body."
    icon = "notebook-pen"
    config_schema = NotionCreatePageConfig
    output_schema = NotionCreatePageResult
    interruptible = False
    provider = "notion"

    async def execute(self, context: NodeContext, config: NotionCreatePageConfig) -> NodeResult:
        try:
            notion = context.services.workspace("notion")
            page = await notion.create_page(config.database_id, config.title, config.content)
        except (ProviderError, ValueError) as exc:
            return NodeResult.fail(str(exc))
        return NodeResult.ok(**page, mock=bool(getattr(notion, "is_mock", False)))


class NotionQueryConfig(NodeConfig):
    database_id: str = Field(min_length=1, description=DATABASE_ID)
    filter_property: str | None = Field(default=None, description="Optional: the property to filter on, e.g. Status. Blank = every page.")
    filter_operator: Literal["equals", "contains"] = "equals"
    filter_value: Any = Field(default=None, description="The value to compare with (text, number, or true/false).")
    max_results: int = Field(default=100, ge=1, le=1000)

    @field_validator("database_id")
    @classmethod
    def _database_id(cls, value: str) -> str:
        return _check_notion_id(value)


class NotionQueryResult(BaseModel):
    pages: list[dict[str, Any]]
    count: int
    data_source_id: str
    mock: bool


@register_node("notion_query_database")
class NotionQueryNode(_Workspace, NodeDefinition[NotionQueryConfig]):
    label = "Notion: Query Database"
    description = "Lists a Notion database's pages, optionally where one property equals or contains a value."
    icon = "notebook-tabs"
    config_schema = NotionQueryConfig
    output_schema = NotionQueryResult
    provider = "notion"

    async def execute(self, context: NodeContext, config: NotionQueryConfig) -> NodeResult:
        prop = (config.filter_property or "").strip() or None
        try:
            notion = context.services.workspace("notion")
            result = await notion.query(
                config.database_id, prop=prop, operator=config.filter_operator, value=config.filter_value,
                max_results=config.max_results,
            )
        except (ProviderError, ValueError) as exc:
            return NodeResult.fail(str(exc))
        return NodeResult.ok(
            pages=result["pages"], count=len(result["pages"]), data_source_id=result["data_source_id"],
            mock=bool(getattr(notion, "is_mock", False)),
        )


# --- Airtable ------------------------------------------------------------------------------


BASE_ID = "The base's id (starts with 'app'; it's in the base's URL)."
TABLE = "The table's name or id (tbl...)."


class AirtableCreateConfig(GuardedOutboundConfig):
    base_id: str = Field(min_length=1, description=BASE_ID)
    table_name: str = Field(min_length=1, description=TABLE)
    fields: dict[str, Any] = Field(description='The record\'s fields by name, e.g. {"Name": "{{input.name}}", "Score": 7}.')
    typecast: bool = Field(default=False, description="Let Airtable convert values (e.g. text to a new select option).")

    @field_validator("base_id")
    @classmethod
    def _base_id(cls, value: str) -> str:
        return _check_base_id(value)


class AirtableCreateResult(BaseModel):
    record_id: str
    created_time: str | None
    fields: dict[str, Any]
    mock: bool


@register_node("airtable_create_record")
class AirtableCreateNode(_Workspace, NodeDefinition[AirtableCreateConfig]):
    guard_fields = ('fields',)
    label = "Airtable: Create Record"
    description = "Adds a record to an Airtable table."
    icon = "table"
    config_schema = AirtableCreateConfig
    output_schema = AirtableCreateResult
    interruptible = False
    provider = "airtable"

    async def execute(self, context: NodeContext, config: AirtableCreateConfig) -> NodeResult:
        if not config.fields:
            return NodeResult.fail("fields is empty: give at least one field, e.g. {\"Name\": \"...\"}")
        try:
            airtable = context.services.workspace("airtable")
            record = await airtable.create_record(config.base_id, config.table_name, config.fields, typecast=config.typecast)
        except ProviderError as exc:
            return NodeResult.fail(str(exc))
        return NodeResult.ok(
            record_id=record["id"], created_time=record.get("createdTime"), fields=record.get("fields") or {},
            mock=bool(getattr(airtable, "is_mock", False)),
        )


class AirtableListConfig(NodeConfig):
    base_id: str = Field(min_length=1, description=BASE_ID)
    table_name: str = Field(min_length=1, description=TABLE)
    filter_formula: str | None = Field(
        default=None, description="Optional Airtable formula, e.g. {Status} = 'Open' or FIND('urgent', {Notes}).",
    )
    view: str | None = Field(default=None, description="Optional view name or id (its filters and order apply).")
    max_records: int = Field(default=100, ge=1, le=1000)

    @field_validator("base_id")
    @classmethod
    def _base_id(cls, value: str) -> str:
        return _check_base_id(value)


class AirtableListResult(BaseModel):
    records: list[dict[str, Any]]
    count: int
    mock: bool


@register_node("airtable_list_records")
class AirtableListNode(_Workspace, NodeDefinition[AirtableListConfig]):
    label = "Airtable: List Records"
    description = "Lists an Airtable table's records, optionally filtered by a formula."
    icon = "table-properties"
    config_schema = AirtableListConfig
    output_schema = AirtableListResult
    provider = "airtable"

    async def execute(self, context: NodeContext, config: AirtableListConfig) -> NodeResult:
        try:
            airtable = context.services.workspace("airtable")
            records = await airtable.list_records(
                config.base_id, config.table_name, formula=(config.filter_formula or "").strip() or None,
                max_records=config.max_records, view=(config.view or "").strip() or None,
            )
        except ProviderError as exc:
            return NodeResult.fail(str(exc))
        rows = [{"id": r.get("id"), "created_time": r.get("createdTime"), "fields": r.get("fields") or {}} for r in records]
        return NodeResult.ok(records=rows, count=len(rows), mock=bool(getattr(airtable, "is_mock", False)))
