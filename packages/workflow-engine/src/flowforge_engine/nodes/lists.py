"""List processing: For Each (a prompt or one LLM call per item), Filter, and Join.

Per-item fields (For Each's prompt and system prompt, Join's template) are resolved once
per item with {{item}} (the item; {{item.title}} one of its fields) and {{index}} (0-based)
in scope, next to the usual references. They are NodeDefinition.deferred_fields, so the
executor leaves them alone and validation accepts {{item...}} there.

For Each is built for free-tier LLMs: at most `concurrency` items are in flight, at most
`rate_limit_per_minute` calls start in any 60-second window, and one item failing is
recorded on that item instead of failing the node.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from flowforge_engine.errors import VariableResolutionError
from flowforge_engine.models import GraphNode, NodeContext, NodeResult
from flowforge_engine.nodes.ai import MAX_FALLBACKS, LLMChainFailed, generate_with_fallback, llm_required_providers, parse_chain_entry
from flowforge_engine.nodes.logic import UNARY_OPERATORS, Operator, evaluate
from flowforge_engine.providers.settings import LLM_PROVIDER_NAMES, LLMProviderName
from flowforge_engine.registry import NodeConfig, NodeDefinition, register_node
from flowforge_engine.variables import build_scope, item_scope, resolve_value, stringify, walk_path

logger = logging.getLogger(__name__)

ITEMS_DESCRIPTION = "A list, usually a reference such as {{gmail_read.emails}} or {{rss.items}}."
FLATTEN_DESCRIPTION = (
    'Merge nested lists one level, to combine sources: items = ["{{gmail_read.emails}}", "{{rss.items}}"].'
)
ITEM_TEMPLATE_HELP = "{{item}} is the item (objects as JSON), {{item.title}} one of its fields, {{index}} its position (0-based)."
# Stop starting new items when less than this is left of the node's time.
DEADLINE_MARGIN_SECONDS = 3.0

# Swappable in tests (a fake clock makes rate limiting instant to test).
_now: Callable[[], float] = time.monotonic
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


def as_list(value: Any, *, flatten: bool = False, field: str = "items") -> list[Any]:
    """The value as a list: a list, a JSON array in text, or nothing (None) -> [].
    Raises ValueError with a hint for anything else."""
    if value is None or value == "":
        items: list[Any] = []
    elif isinstance(value, list):
        items = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            raise ValueError(f"'{field}' must be a list (got text that isn't a JSON array)") from None
        if not isinstance(parsed, list):
            raise ValueError(f"'{field}' must be a list (got JSON {type(parsed).__name__})")
        items = parsed
    elif isinstance(value, dict):
        lists = [key for key, item in value.items() if isinstance(item, list)]
        hint = f"; did you mean one of its lists: {', '.join(lists)}?" if lists else ""
        raise ValueError(f"'{field}' must be a list, got an object{hint}")
    else:
        raise ValueError(f"'{field}' must be a list, got {type(value).__name__}")
    if not flatten:
        return list(items)
    flat: list[Any] = []
    for item in items:
        if isinstance(item, list):
            flat.extend(item)
        elif item is not None:
            flat.append(item)
    return flat


class RateLimiter:
    """At most `per_minute` acquisitions in any `window` seconds (0 = unlimited). Waiters
    queue in order; `acquire` returns False instead of waiting past `deadline`."""

    def __init__(
        self,
        per_minute: int,
        *,
        window: float = 60.0,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ):
        self.per_minute = per_minute
        self.window = window
        self._clock = clock or _now
        self._sleep = sleep or _sleep
        self._starts: deque[float] = deque()
        self._lock = asyncio.Lock()
        self.waited = 0.0

    async def acquire(self, deadline: float | None = None) -> bool:
        if self.per_minute <= 0:
            return True
        async with self._lock:
            while True:
                now = self._clock()
                while self._starts and now - self._starts[0] >= self.window:
                    self._starts.popleft()
                if len(self._starts) < self.per_minute:
                    self._starts.append(now)
                    return True
                wait = self.window - (now - self._starts[0])
                if deadline is not None and now + wait > deadline:
                    return False
                self.waited += wait
                await self._sleep(wait)


def parse_json_reply(reply: str) -> Any:
    """A JSON object or array from an LLM reply (code fences and surrounding prose are
    tolerated). Raises ValueError."""
    body = reply.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", body, re.DOTALL)
    if fenced:
        body = fenced.group(1).strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    for opening, closing in (("{", "}"), ("[", "]")):
        start, end = body.find(opening), body.rfind(closing)
        if start != -1 and end > start:
            try:
                return json.loads(body[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("the reply is not valid JSON")


# --- For Each ------------------------------------------------------------------------------


class ForEachConfig(NodeConfig):
    items: Any = Field(description=ITEMS_DESCRIPTION)
    flatten: bool = Field(default=False, description=FLATTEN_DESCRIPTION)
    mode: Literal["llm", "template"] = Field(
        default="llm",
        description="llm: send the prompt for each item to the LLM and collect the replies. template: just render the prompt per item (no LLM).",
    )
    prompt: str = Field(min_length=1, description=f"Applied to each item. {ITEM_TEMPLATE_HELP}")
    system_prompt: str = Field(default="", description="LLM mode: the same for every item (may use {{item}} too).")
    output_format: Literal["text", "json"] = Field(
        default="text",
        description="json: parse each reply as JSON (ask for it in the prompt), so later nodes can read fields like {{item.output.score}}.",
    )
    provider: LLMProviderName = Field(default="gemini", description="LLM mode: which service answers.")
    model: str | None = Field(default=None, description="Blank uses the provider's default model.")
    fallback: list[str] = Field(
        default_factory=list, max_length=MAX_FALLBACKS,
        description="Providers to try per item if this one fails, each 'provider' or 'provider:model'.",
    )
    temperature: float = Field(default=0.3, ge=0, le=2)
    max_tokens: int = Field(default=1024, ge=1, le=65536)
    concurrency: int = Field(default=2, ge=1, le=10, description="Items processed at the same time.")
    rate_limit_per_minute: int = Field(
        default=10, ge=0, le=1000,
        description="At most this many LLM calls start in any minute (0 = no limit). Keep it under your provider's free-tier limit.",
    )
    max_items: int = Field(default=25, ge=1, le=500, description="Items beyond this are skipped (reported as skipped).")
    item_timeout_seconds: float = Field(default=90, gt=0, le=600, description="Longest wait for one item's answer.")
    timeout_seconds: float = Field(
        default=600, gt=0, le=3600,
        description="The whole node's time budget. Items not started when it runs out are marked not run.",
    )
    fail_when: Literal["all_failed", "any_failed", "never"] = Field(
        default="all_failed",
        description="When the node itself fails. Otherwise failed items are recorded per item and the run goes on.",
    )

    @field_validator("fallback")
    @classmethod
    def _known_providers(cls, entries: list[str]) -> list[str]:
        for entry in entries:
            provider, _ = parse_chain_entry(entry)
            if provider not in LLM_PROVIDER_NAMES:
                raise ValueError(f"unknown provider '{provider}' (known: {', '.join(LLM_PROVIDER_NAMES)})")
        return entries


class ForEachResult(BaseModel):
    results: list[dict[str, Any]]
    outputs: list[Any]
    count: int
    succeeded: int
    failed: int
    skipped: int
    mode: str
    rate_limited_seconds: float


@register_node("for_each")
class ForEachNode(NodeDefinition[ForEachConfig]):
    category = "lists"
    label = "For Each"
    description = "Runs a prompt (or one LLM call) per list item, with concurrency and per-minute rate limits."
    icon = "repeat"
    config_schema = ForEachConfig
    output_schema = ForEachResult
    queue = "llm"
    deferred_fields = frozenset({"prompt", "system_prompt"})

    def required_providers(self, node: GraphNode) -> list[tuple[str, str]]:
        # Template mode calls no LLM (a templated mode is checked as if it might).
        if node.config.get("mode", "llm") == "template":
            return []
        return llm_required_providers(node, "gemini")

    def timeout(self, config: ForEachConfig, default: float) -> float:
        return config.timeout_seconds

    async def execute(self, context: NodeContext, config: ForEachConfig) -> NodeResult:
        try:
            items = as_list(config.items, flatten=config.flatten)
        except ValueError as exc:
            return NodeResult.fail(str(exc))
        selected = items[: config.max_items]
        skipped = len(items) - len(selected)
        scope = build_scope(context)
        llm = config.mode == "llm"
        limiter = RateLimiter(config.rate_limit_per_minute if llm else 0)
        slots = asyncio.Semaphore(config.concurrency if llm else 1)
        deadline = context.deadline

        async def process(index: int, item: Any) -> dict[str, Any]:
            record: dict[str, Any] = {"index": index, "item": item, "ok": False, "output": None, "error": None}
            local = item_scope(scope, item, index)
            try:
                prompt = stringify(resolve_value(config.prompt, local))
                system = stringify(resolve_value(config.system_prompt, local))
            except VariableResolutionError as exc:
                record["error"] = str(exc)
                return record
            if not llm:
                record.update(ok=True, output=prompt)
                return record
            async with slots:
                budget_end = None if deadline is None else deadline - DEADLINE_MARGIN_SECONDS
                if not await limiter.acquire(budget_end):
                    record["error"] = "Not run: the rate limit would have delayed it past the node's time budget"
                    return record
                started = _now()
                if budget_end is not None and started >= budget_end:
                    record["error"] = "Not run: the node's time budget ran out"
                    return record
                limit = config.item_timeout_seconds
                if budget_end is not None:
                    limit = min(limit, budget_end - started)
                item_context = context.model_copy(update={"deadline": time.monotonic() + limit, "on_token": None})
                try:
                    answer = await asyncio.wait_for(
                        generate_with_fallback(
                            item_context,
                            provider=config.provider,
                            model=config.model,
                            fallback=config.fallback,
                            system_prompt=system,
                            user_prompt=prompt,
                            temperature=config.temperature,
                            max_tokens=config.max_tokens,
                        ),
                        timeout=limit,
                    )
                except LLMChainFailed as exc:
                    record["error"] = str(exc)
                    return record
                except TimeoutError:
                    record["error"] = f"No answer within {limit:.0f}s"
                    return record
            record.update(provider_used=answer.provider_used, model=answer.model)
            if config.output_format == "json":
                try:
                    record["output"] = parse_json_reply(answer.text)
                except ValueError as exc:
                    record["error"] = f"{exc}: {answer.text[:300]}"
                    return record
            else:
                record["output"] = answer.text.strip()
            record["ok"] = True
            return record

        results = list(await asyncio.gather(*(process(i, item) for i, item in enumerate(selected))))
        succeeded = sum(1 for r in results if r["ok"])
        failed = len(results) - succeeded
        output = {
            "results": results,
            "outputs": [r["output"] for r in results if r["ok"]],
            "count": len(results),
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "mode": config.mode,
            "rate_limited_seconds": round(limiter.waited, 1),
        }
        logger.info(
            "for each finished",
            extra={"node_id": context.node_id, "items": len(results), "failed": failed, "skipped": skipped},
        )
        first_error = next((r["error"] for r in results if not r["ok"]), None)
        if config.fail_when == "all_failed" and results and not succeeded:
            return NodeResult.fail(f"All {len(results)} items failed; the first: {first_error}", **output)
        if config.fail_when == "any_failed" and failed:
            return NodeResult.fail(f"{failed} of {len(results)} items failed; the first: {first_error}", **output)
        return NodeResult(success=True, output=output)


# --- Filter ----------------------------------------------------------------------------------


class FilterConfig(NodeConfig):
    items: Any = Field(description=ITEMS_DESCRIPTION)
    flatten: bool = Field(default=False, description=FLATTEN_DESCRIPTION)
    field: str = Field(
        default="",
        description="What to compare in each item: a path such as output.score, title, or item.from_address. Blank = the item itself.",
    )
    operator: Operator
    value: Any = Field(default=None, description="Compared with the field (not used by is_empty / is_not_empty).")
    case_sensitive: bool = Field(default=False, description="Off: text compares ignoring case and surrounding spaces.")


class FilterResult(BaseModel):
    items: list[Any]
    count: int
    removed: int
    errors: list[dict[str, Any]]


@register_node("filter")
class FilterNode(NodeDefinition[FilterConfig]):
    category = "lists"
    label = "Filter"
    description = "Keeps the list items whose field matches a condition (e.g. output.score >= 70)."
    icon = "filter"
    config_schema = FilterConfig
    output_schema = FilterResult
    portable = True

    async def execute(self, context: NodeContext, config: FilterConfig) -> NodeResult:
        try:
            items = as_list(config.items, flatten=config.flatten)
        except ValueError as exc:
            return NodeResult.fail(str(exc))
        path = config.field.strip()
        if path == "item":
            path = ""
        elif path.startswith("item."):
            path = path[len("item."):]
        kept: list[Any] = []
        errors: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            try:
                value = walk_path(item, path)
            except VariableResolutionError:
                value = None  # a missing field: only is_empty matches it
                if config.operator not in UNARY_OPERATORS:
                    continue
            try:
                if evaluate(value, config.operator, config.value, case_sensitive=config.case_sensitive):
                    kept.append(item)
            except ValueError as exc:
                if len(errors) < 20:
                    errors.append({"index": index, "error": str(exc)})
        return NodeResult.ok(items=kept, count=len(kept), removed=len(items) - len(kept), errors=errors)


# --- Join / Format ---------------------------------------------------------------------------


def _unescape(text: str) -> str:
    """Form fields can't hold a real newline in one line: accept \\n and \\t."""
    return text.replace("\\n", "\n").replace("\\t", "\t")


class JoinConfig(NodeConfig):
    items: Any = Field(description=ITEMS_DESCRIPTION)
    flatten: bool = Field(default=False, description=FLATTEN_DESCRIPTION)
    template: str = Field(default="", description=f"How each item reads. {ITEM_TEMPLATE_HELP} Blank = the item itself.")
    separator: str = Field(default="\\n", description="Between items. \\n is a line break.")
    numbered: bool = Field(default=False, description='Prefix each item with "1. ", "2. ", ...')
    header: str = Field(default="", description="Text before the list.")
    footer: str = Field(default="", description="Text after the list.")
    empty_text: str = Field(default="", description="The whole text when the list is empty.")
    max_items: int = Field(default=200, ge=1, le=10_000)
    max_chars: int = Field(default=100_000, ge=100, le=1_000_000)


class JoinResult(BaseModel):
    text: str
    count: int
    truncated: bool


@register_node("join")
class JoinNode(NodeDefinition[JoinConfig]):
    category = "lists"
    label = "Join / Format"
    description = "Turns a list into one text block: each item through a template, joined by a separator."
    icon = "list-collapse"
    config_schema = JoinConfig
    output_schema = JoinResult
    portable = True
    deferred_fields = frozenset({"template"})

    async def execute(self, context: NodeContext, config: JoinConfig) -> NodeResult:
        try:
            items = as_list(config.items, flatten=config.flatten)
        except ValueError as exc:
            return NodeResult.fail(str(exc))
        if not items:
            return NodeResult.ok(text=_unescape(config.empty_text), count=0, truncated=False)
        scope = build_scope(context)
        parts = []
        for index, item in enumerate(items[: config.max_items]):
            if config.template.strip():
                try:
                    part = stringify(resolve_value(_unescape(config.template), item_scope(scope, item, index)))
                except VariableResolutionError as exc:
                    return NodeResult.fail(f"Item {index}: {exc}")
            else:
                part = stringify(item)
            parts.append(f"{index + 1}. {part}" if config.numbered else part)
        body = _unescape(config.separator).join(parts)
        text = "\n".join(filter(None, [_unescape(config.header).rstrip("\n"), body, _unescape(config.footer).lstrip("\n")]))
        truncated = len(items) > config.max_items or len(text) > config.max_chars
        return NodeResult.ok(text=text[: config.max_chars], count=len(parts), truncated=truncated)
