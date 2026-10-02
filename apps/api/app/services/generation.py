"""Generate with AI: a plain-English description becomes a real, validated pipeline.

An LLM gets a compact catalog of every node type (built from the registry, the same schemas
GET /api/nodes serves), the reference syntax, and the request, and answers a graph as JSON.
The reply is checked rather than trusted: it must parse, use only node types that exist,
wire edges between nodes it declared, and pass the workflow validator (config schemas,
`{{...}}` references, branches). LLM steps are then pointed at the providers the user has
keys for (the templates' fit_graph). Any problem goes back to the model with the exact
messages, up to MAX_ATTEMPTS times; only a graph that validates is saved.
"""

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from flowforge_engine import ExecutionServices, GraphError, WorkflowGraph, default_registry, topological_sort, validate_workflow
from flowforge_engine.errors import ProviderError
from flowforge_engine.nodes.lists import parse_json_reply
from pydantic import ValidationError

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
LLMCall = Callable[[str, str], Awaitable[str]]

# Shown first and described in more detail: what most pipelines are made of.
COMMON = ("input", "output", "text", "gemini", "structured_output", "condition", "for_each", "join",
          "gmail", "gmail_read", "telegram", "web_search", "web_page", "ocr", "pdf_extract", "extract_entities",
          "summarize", "rss", "http_request")


# The fields of each item in list outputs, for {{item.<field>}} in for_each / join / filter
# templates (validation can't check those: they're resolved per item at run time).
ITEM_FIELDS = {
    "gmail_read": "emails[]: from, from_address, to, subject, date, snippet, body_text, attachments",
    "web_search": "results[]: title, url, snippet (and page_title, page_text when fetch_pages > 0)",
    "rss": "items[]: title, link, summary, published, author",
    "retriever": "results[]: rank, citation, score, content, filename, page",
    "notion_query_database": "pages[]: id, url, properties",
    "airtable_list_records": "records[]: id, fields",
    "for_each": "outputs[]: one answer per item (text, or parsed JSON with output_format json)",
    "filter": "items[]: the kept items",
}


class GenerationFailed(Exception):
    def __init__(self, problems: list[str], draft: dict[str, Any] | None, attempts: int):
        self.problems, self.draft, self.attempts = problems, draft, attempts
        super().__init__("; ".join(problems[:5]))


@dataclass
class Generated:
    name: str
    description: str
    graph: dict[str, Any]
    attempts: int
    warnings: list[str] = field(default_factory=list)


# --- The catalog ------------------------------------------------------------------------------


def _type_of(prop: dict[str, Any], defs: dict[str, Any]) -> str:
    if "$ref" in prop:
        return _type_of(defs.get(prop["$ref"].rsplit("/", 1)[-1], {}), defs)
    if "enum" in prop:
        return "|".join(json.dumps(v) for v in prop["enum"])
    if "anyOf" in prop:
        return " or ".join(dict.fromkeys(_type_of(p, defs) for p in prop["anyOf"] if p.get("type") != "null")) or "any"
    kind = prop.get("type")
    if kind == "array":
        return f"list of {_type_of(prop.get('items', {}), defs)}"
    if kind == "object" and prop.get("properties"):
        inner = ", ".join(f"{k}: {_type_of(v, defs)}" for k, v in prop["properties"].items())
        return "{" + inner + "}"
    return kind or "any"


def node_catalog() -> str:
    """Every node type as a few lines: what it does, its config fields, and its outputs."""
    order = list(COMMON) + sorted(t for t in default_registry.types() if t not in COMMON)
    lines = []
    for node_type in order:
        definition = default_registry.get(node_type)
        if definition is None:
            continue
        schema = definition.config_schema.model_json_schema()
        defs, required = schema.get("$defs", {}), set(schema.get("required", []))
        fields = []
        for name, prop in schema.get("properties", {}).items():
            if name == "privacy_guard":
                continue
            text = f"{name}: {_type_of(prop, defs)}"
            if name in required:
                text += " (required)"
            elif "default" in prop and prop["default"] not in (None, "", [], {}):
                text += f" = {json.dumps(prop['default'])}"
            description = (prop.get("description") or "").split(". ")[0][:90]
            if description and node_type in COMMON:
                text += f" — {description}"
            fields.append(text)
        keys = definition.output_keys(definition_node(node_type))
        outputs = "value, <name>" if node_type == "input" else (", ".join(sorted(keys)) if keys else "varies")
        if node_type in ITEM_FIELDS:
            outputs += f"; each item of {ITEM_FIELDS[node_type]}"
        branch = f" Branches: {', '.join(definition.branches)} (edge source_handle)." if definition.branches else ""
        lines.append(
            f"- {node_type}: {definition.description}{branch}\n  config: {'; '.join(fields) or 'none'}\n  outputs: {outputs}"
        )
    return "\n".join(lines)


def definition_node(node_type: str) -> Any:
    from flowforge_engine import GraphNode

    return GraphNode(id="node", type=node_type)


def generation_prompt(request: str, *, my_email: str | None) -> str:
    me = f"The user's own email address is {my_email} (use it when they say 'email me').\n" if my_email else ""
    return f"""Design a FlowForge pipeline for this request.

Request: {request}

Reply with one JSON object:
{{"name": "short title", "description": "one sentence", "nodes": [{{"id": "snake_case_id", "type": "<node type>", "label": "Short label", "config": {{...}}}}], "edges": [{{"source": "id", "target": "id", "source_handle": null}}]}}

Rules:
- Use only node types from the catalog below, with the config fields it lists (no others). Fill required fields.
- Data flows through references: "{{{{node_id.output_key}}}}" (e.g. "{{{{search.results}}}}", "{{{{gemini.response}}}}"). An Input node with id "topic" is referenced as "{{{{topic.value}}}}". A reference may only point at a node upstream of it (connected by edges).
- Start with Input nodes for what the user provides (input_type "file" for an uploaded document or image, with no default) and end with an Output node whose value is the useful content (the summary, the extracted data, the answer), never a status or timestamp. Give text inputs a realistic default so the pipeline can run as is.
- For an LLM step use type "gemini" with provider "gemini" (it is switched to whatever provider the user has). Write clear prompts that include the referenced data.
- To process each item of a list with an LLM, use for_each (its prompt uses {{{{item}}}} or {{{{item.<field>}}}}), then join the outputs. Use only the item fields the catalog lists for that list.
- Every edge connects declared nodes; add an edge for every reference. A condition's outgoing edges set source_handle "true" or "false".
- Telegram: leave chat_id empty (the user's default chat). Gmail send: subject and body are required.
{me}
Node catalog:
{node_catalog()}
"""


# --- Checking a reply -----------------------------------------------------------------------


def _layout(graph: dict[str, Any]) -> None:
    """Left-to-right columns by depth, so the editor opens on a readable canvas."""
    nodes = graph["nodes"]
    depth = {n["id"]: 0 for n in nodes}
    try:
        model = WorkflowGraph.model_validate(graph)
        order = topological_sort(model.nodes, model.edges)
    except (GraphError, ValidationError):
        order = [n["id"] for n in nodes]
    incoming: dict[str, list[str]] = {}
    for edge in graph["edges"]:
        incoming.setdefault(edge["target"], []).append(edge["source"])
    for node_id in order:
        depth[node_id] = max((depth[s] + 1 for s in incoming.get(node_id, []) if s in depth), default=0)
    rows: dict[int, int] = {}
    for node in sorted(nodes, key=lambda n: order.index(n["id"]) if n["id"] in order else 0):
        column = depth.get(node["id"], 0)
        node["position"] = {"x": 320 * column, "y": 160 * rows.get(column, 0)}
        rows[column] = rows.get(column, 0) + 1


def normalize(data: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """(graph dict, problems) from the model's JSON: structure only; validation comes after."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return None, ["The reply must be one JSON object with nodes and edges"]
    raw_nodes, raw_edges = data.get("nodes"), data.get("edges", [])
    if not isinstance(raw_nodes, list) or not raw_nodes:
        return None, ["`nodes` must be a non-empty list"]
    known = set(default_registry.types())
    nodes, ids = [], set()
    for item in raw_nodes:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("type"), str):
            problems.append("Every node needs a string `id` and `type`")
            continue
        node_id = re.sub(r"[^A-Za-z0-9_-]", "_", item["id"].strip()) or "node"
        if node_id in ids:
            problems.append(f"Duplicate node id '{node_id}'")
            continue
        if item["type"] not in known:
            problems.append(f"Node '{node_id}' uses type '{item['type']}', which doesn't exist; use only catalog types")
        ids.add(node_id)
        config = dict(item.get("config")) if isinstance(item.get("config"), dict) else {}
        if item["type"] == "input" and config.get("input_type") == "file":
            config.pop("default", None)  # an invented "sample.pdf" isn't an upload; the run form asks for one
        nodes.append({"id": node_id, "type": item["type"], "label": str(item.get("label") or "")[:100] or None,
                      "config": config})
    edges = []
    for edge in raw_edges if isinstance(raw_edges, list) else []:
        if not isinstance(edge, dict) or edge.get("source") not in ids or edge.get("target") not in ids:
            problems.append(f"Edge {json.dumps(edge)[:120]} connects nodes that weren't declared")
            continue
        edges.append({"source": edge["source"], "target": edge["target"], "source_handle": edge.get("source_handle")})
    return {"nodes": nodes, "edges": edges, "variables": []}, problems


def check(graph: dict[str, Any], services: ExecutionServices) -> list[str]:
    """Validator problems that the model can fix (missing credentials aren't its fault)."""
    try:
        model = WorkflowGraph.model_validate(graph)
    except ValidationError as exc:
        return [f"Invalid graph: {e['msg']} at {'.'.join(map(str, e['loc']))}" for e in exc.errors()[:10]]
    return [
        f"{issue.message}" for issue in validate_workflow(model, services=services)
        if issue.code.value != "auth_missing"
    ]


async def generate(
    llm: LLMCall, request: str, services: ExecutionServices, *, my_email: str | None = None,
    fit: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> Generated:
    """Ask, check, and retry with the problems until the graph validates."""
    prompt = generation_prompt(request, my_email=my_email)
    problems: list[str] = []
    draft: dict[str, Any] | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        message = prompt if attempt == 1 else (
            f"{prompt}\n\nYour previous answer had these problems; fix every one and reply with the whole corrected "
            f"JSON:\n- " + "\n- ".join(problems[:15]) + f"\n\nPrevious answer:\n{json.dumps(draft)[:6000]}"
        )
        reply = await llm("You design automation pipelines. You reply with JSON only.", message)
        try:
            data = parse_json_reply(reply)
        except ValueError as exc:
            problems, draft = [f"The reply wasn't JSON: {exc}"], None
            continue
        graph, problems = normalize(data)
        draft = graph
        if graph is None or problems:
            continue
        if fit is not None:
            graph = fit(graph)
        problems = check(graph, services)
        draft = graph
        if not problems:
            _layout(graph)
            model = WorkflowGraph.model_validate(graph)
            warnings = [i.message for i in validate_workflow(model, services=services) if i.code.value == "auth_missing"]
            name = str(data.get("name") or "Generated pipeline").strip()[:120]
            description = str(data.get("description") or "").strip()[:500]
            logger.info("pipeline generated", extra={"attempts": attempt, "nodes": len(graph["nodes"])})
            return Generated(name, description, graph, attempt, warnings)
        logger.info("generated graph rejected; retrying", extra={"attempt": attempt, "problems": problems[:5]})
    raise GenerationFailed(problems, draft, MAX_ATTEMPTS)


def owner_llm(services: ExecutionServices) -> LLMCall:
    """The user's free LLMs in order (Gemini, Groq, OpenRouter), each tried in turn."""

    async def call(system: str, prompt: str) -> str:
        names = ["mock"] if services.settings.testing else [n for n in ("gemini", "groq", "openrouter") if services.has_credentials(n)]
        errors = []
        for name in names:
            try:
                return await services.llm(name).generate(system, prompt, services.default_model(name), 0.2, 8192)
            except ProviderError as exc:
                errors.append(str(exc))
        raise ProviderError("generator", "; ".join(errors) or "no LLM key is configured (Gemini, Groq, or OpenRouter)")

    return call
