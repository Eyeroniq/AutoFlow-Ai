"""{{...}} template resolution.

References are dot paths: the first segment is a node id (its output), `vars` (workflow
variables), or `system` (execution metadata). Later segments walk into dicts; list items
are addressed with `.0` or `[0]`.

    {{gemini.response}}   {{http.body.items[0].name}}   {{vars.recipient}}   {{system.execution_id}}

A string that is exactly one reference resolves to the referenced value with its type
intact (a dict stays a dict); references embedded in text are stringified.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

from flowforge_engine.errors import VariableResolutionError
from flowforge_engine.models import NodeContext

RESERVED_NAMESPACES = frozenset({"vars", "system"})
SYSTEM_KEYS = frozenset({"workflow_id", "execution_id", "node_id"})
# Available only in per-item template fields (NodeDefinition.deferred_fields): the current
# list item and its 0-based position. They shadow a node with the same id there.
ITEM_NAMESPACES = frozenset({"item", "index"})

_TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_SEGMENT_RE = re.compile(r"^([^\[\]]*)((?:\[\d+\])*)$")
_INDEX_RE = re.compile(r"\[(\d+)\]")
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

PathSegment = str | int


def parse_reference(expression: str) -> list[PathSegment]:
    """Split "a.b[0].c" into ["a", "b", 0, "c"]. Raises VariableResolutionError on bad syntax."""
    expr = expression.strip()
    if not expr:
        raise VariableResolutionError(expression, "empty reference")

    path: list[PathSegment] = []
    for raw in expr.split("."):
        match = _SEGMENT_RE.match(raw)
        name = match.group(1) if match else None
        if match is None or (name == "" and not path) or (name and not _NAME_RE.match(name)):
            raise VariableResolutionError(expression, f"invalid path segment '{raw}'")
        if name == "" and not match.group(2):
            raise VariableResolutionError(expression, "empty path segment")
        if name:
            path.append(name)
        path.extend(int(i) for i in _INDEX_RE.findall(match.group(2)))
    return path


def build_scope(context: NodeContext) -> dict[str, Any]:
    """Everything a node's config may reference."""
    return {
        **context.node_outputs,
        "vars": context.variables,
        "system": {
            "workflow_id": context.workflow_id,
            "execution_id": context.execution_id,
            "node_id": context.node_id,
        },
    }


def item_scope(scope: Mapping[str, Any], item: Any, index: int) -> dict[str, Any]:
    """`scope` plus {{item}} and {{index}}, for resolving a per-item template."""
    return {**scope, "item": item, "index": index}


def walk_path(value: Any, path: str) -> Any:
    """Read a dot path ("output.score", "tags[0]") inside `value`.

    Raises VariableResolutionError when a segment is missing. An empty path is `value`.
    """
    if not path.strip():
        return value
    return lookup({"_": value}, f"_.{path.strip()}")


def lookup(scope: Mapping[str, Any], expression: str) -> Any:
    path = parse_reference(expression)
    root = path[0]
    if root not in scope:
        raise VariableResolutionError(
            expression,
            f"'{root}' has no output in this run (not a node id, 'vars', or 'system'; "
            "or the node was skipped or hasn't run yet)",
        )

    current: Any = scope[root]
    walked = str(root)
    for segment in path[1:]:
        if isinstance(current, Mapping):
            key = str(segment)
            if key not in current:
                available = ", ".join(sorted(map(str, current))) or "none"
                raise VariableResolutionError(
                    expression, f"key '{key}' not found in {walked} (available: {available})"
                )
            current = current[key]
        elif isinstance(current, list):
            index = segment if isinstance(segment, int) else _as_index(segment)
            if index is None:
                raise VariableResolutionError(expression, f"{walked} is a list; use an index, not '{segment}'")
            if index >= len(current):
                raise VariableResolutionError(
                    expression, f"index {index} out of range for {walked} (length {len(current)})"
                )
            current = current[index]
        else:
            raise VariableResolutionError(
                expression, f"cannot read '{segment}' from {walked} ({type(current).__name__} value)"
            )
        walked += f"[{segment}]" if isinstance(segment, int) else f".{segment}"
    return current


def _as_index(segment: str) -> int | None:
    return int(segment) if segment.isdigit() else None


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def resolve_string(template: str, scope: Mapping[str, Any]) -> Any:
    matches = list(_TEMPLATE_RE.finditer(template))
    if not matches:
        return template
    # Exactly one reference and nothing else: keep the value's type.
    if len(matches) == 1 and matches[0].group(0) == template.strip():
        return lookup(scope, matches[0].group(1))
    return _TEMPLATE_RE.sub(lambda m: stringify(lookup(scope, m.group(1))), template)


def resolve_value(value: Any, scope: Mapping[str, Any]) -> Any:
    """Resolve references anywhere inside strings, dicts, and lists."""
    if isinstance(value, str):
        return resolve_string(value, scope)
    if isinstance(value, dict):
        return {key: resolve_value(item, scope) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_value(item, scope) for item in value]
    return value


def iter_references(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Yield (field_path, expression) for every reference in a config value."""
    if isinstance(value, str):
        for match in _TEMPLATE_RE.finditer(value):
            yield path, match.group(1).strip()
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from iter_references(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from iter_references(item, f"{path}[{index}]")


def find_references(value: Any) -> list[str]:
    return [expression for _, expression in iter_references(value)]


def contains_reference(value: Any) -> bool:
    return isinstance(value, str) and _TEMPLATE_RE.search(value) is not None
