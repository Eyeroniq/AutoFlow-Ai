from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

import pydantic

from flowforge_engine.errors import CycleError, GraphError, VariableResolutionError
from flowforge_engine.models import (
    GraphEdge,
    GraphNode,
    GraphVariable,
    IssueCode,
    ValidationIssue,
    WorkflowGraph,
)
from flowforge_engine.registry import NodeDefinition, NodeRegistry, default_registry
from flowforge_engine.variables import (
    RESERVED_NAMESPACES,
    SYSTEM_KEYS,
    contains_reference,
    iter_references,
    parse_reference,
)

# --- Ordering ---------------------------------------------------------------------------


def topological_sort(nodes: Sequence[GraphNode], edges: Iterable[GraphEdge]) -> list[str]:
    """Node ids in dependency order. Ties keep the order nodes were declared in.

    Raises GraphError for edges that reference unknown nodes and CycleError for cycles.
    """
    position = {node.id: index for index, node in enumerate(nodes)}
    adjacency: dict[str, list[str]] = defaultdict(list)
    in_degree = dict.fromkeys(position, 0)

    for edge in edges:
        for endpoint in (edge.source, edge.target):
            if endpoint not in position:
                raise GraphError(f"Edge '{edge.id}' references unknown node '{endpoint}'")
        adjacency[edge.source].append(edge.target)
        in_degree[edge.target] += 1

    ready = [(position[n], n) for n, degree in in_degree.items() if degree == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        _, node_id = heapq.heappop(ready)
        order.append(node_id)
        for target in adjacency[node_id]:
            in_degree[target] -= 1
            if in_degree[target] == 0:
                heapq.heappush(ready, (position[target], target))

    if len(order) < len(position):
        done = set(order)
        remaining = [n for n in position if n not in done]
        raise CycleError(_find_cycle(remaining, adjacency) or remaining)
    return order


def _find_cycle(node_ids: Iterable[str], adjacency: dict[str, list[str]]) -> list[str] | None:
    """Return one cycle as [a, b, ..., a], or None. Iterative DFS (no recursion limit)."""
    white, grey, black = 0, 1, 2
    color: dict[str, int] = defaultdict(int)
    for start in node_ids:
        if color[start] != white:
            continue
        stack: list[tuple[str, Iterable[str]]] = [(start, iter(adjacency[start]))]
        path = [start]
        color[start] = grey
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                color[node] = black
                stack.pop()
                path.pop()
            elif color[child] == grey:
                return path[path.index(child):] + [child]
            elif color[child] == white:
                color[child] = grey
                stack.append((child, iter(adjacency[child])))
                path.append(child)
    return None


def _ancestors(node_ids: Iterable[str], edges: Iterable[GraphEdge]) -> dict[str, set[str]]:
    parents: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        parents[edge.target].add(edge.source)
    result: dict[str, set[str]] = {}
    for node_id in node_ids:
        seen: set[str] = set()
        frontier = list(parents[node_id])
        while frontier:
            current = frontier.pop()
            if current not in seen:
                seen.add(current)
                frontier.extend(parents[current])
        result[node_id] = seen
    return result


# --- Validation -------------------------------------------------------------------------


def validate_workflow(graph: WorkflowGraph, *, registry: NodeRegistry | None = None) -> list[ValidationIssue]:
    return validate_graph(graph.nodes, graph.edges, graph.variables, registry=registry)


def validate_graph(
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge],
    variables: Sequence[GraphVariable] = (),
    *,
    registry: NodeRegistry | None = None,
) -> list[ValidationIssue]:
    """Every problem that would stop the graph from running. Empty list = valid."""
    registry = registry or default_registry
    issues: list[ValidationIssue] = []

    # Node ids
    nodes_by_id: dict[str, GraphNode] = {}
    for node in nodes:
        if node.id in nodes_by_id:
            issues.append(ValidationIssue(
                code=IssueCode.DUPLICATE_NODE_ID, node_id=node.id,
                message=f"Node id '{node.id}' is used more than once",
            ))
            continue
        nodes_by_id[node.id] = node
        if node.id in RESERVED_NAMESPACES:
            issues.append(ValidationIssue(
                code=IssueCode.RESERVED_NODE_ID, node_id=node.id,
                message=f"Node id '{node.id}' is reserved for {{{{{node.id}.*}}}} references; rename the node",
            ))

    # Node types and config
    definitions: dict[str, NodeDefinition[Any]] = {}
    for node in nodes_by_id.values():
        definition = registry.get(node.type)
        if definition is None:
            issues.append(ValidationIssue(
                code=IssueCode.UNKNOWN_NODE_TYPE, node_id=node.id, field="type",
                message=f"Node '{node.id}' has unknown type '{node.type}' (known: {', '.join(registry.types())})",
            ))
            continue
        definitions[node.id] = definition
        issues.extend(_config_issues(node, definition))

    # Variables
    variable_keys: set[str] = set()
    for variable in variables:
        if variable.key in variable_keys:
            issues.append(ValidationIssue(
                code=IssueCode.DUPLICATE_VARIABLE, field=variable.key,
                message=f"Variable '{variable.key}' is defined more than once",
            ))
        variable_keys.add(variable.key)

    # Edges
    valid_edges: list[GraphEdge] = []
    for edge in edges:
        broken = [end for end in (edge.source, edge.target) if end not in nodes_by_id]
        for end in broken:
            role = "source" if end == edge.source else "target"
            issues.append(ValidationIssue(
                code=IssueCode.BROKEN_EDGE, edge_id=edge.id,
                message=f"Edge '{edge.id}' {role} '{end}' is not a node in this graph",
            ))
        if broken:
            continue
        valid_edges.append(edge)
        source_def = definitions.get(edge.source)
        if source_def and source_def.branches and edge.source_handle not in source_def.branches:
            issues.append(ValidationIssue(
                code=IssueCode.INVALID_BRANCH, edge_id=edge.id, node_id=edge.source,
                message=(
                    f"Edge '{edge.id}' leaves branching node '{edge.source}' without a branch handle; "
                    f"set source_handle to one of: {', '.join(source_def.branches)}"
                ),
            ))

    # Cycles (every cycle is rejected; loops arrive as an explicit construct later)
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in valid_edges:
        adjacency[edge.source].append(edge.target)
    cycle = _find_cycle(nodes_by_id, adjacency)
    if cycle:
        issues.append(ValidationIssue(
            code=IssueCode.CYCLE, node_id=cycle[0],
            message="Workflow contains a cycle: " + " → ".join(cycle),
        ))

    # {{...}} references
    ancestors = _ancestors(nodes_by_id, valid_edges)
    for node in nodes_by_id.values():
        if node.id not in definitions:
            continue
        for field, expression in iter_references(node.config):
            problem = _reference_problem(expression, node, nodes_by_id, definitions, ancestors, variable_keys)
            if problem:
                issues.append(ValidationIssue(
                    code=IssueCode.UNRESOLVABLE_REFERENCE, node_id=node.id, field=field or None,
                    message=f"Node '{node.id}' references '{{{{{expression}}}}}': {problem}",
                ))

    issues.extend(_duplicate_name_issues(nodes_by_id.values(), definitions))
    return issues


def _config_issues(node: GraphNode, definition: NodeDefinition[Any]) -> list[ValidationIssue]:
    try:
        definition.config_schema.model_validate(node.config)
    except pydantic.ValidationError as exc:
        issues = []
        for error in exc.errors():
            missing = error["type"] == "missing"
            # A templated value ("{{vars.temperature}}") can only be checked at run time.
            if not missing and contains_reference(error.get("input")):
                continue
            field = ".".join(str(part) for part in error["loc"]) or None
            if missing:
                message = f"Node '{node.id}' ({node.type}) is missing required config field '{field}'"
            elif error["type"] == "extra_forbidden":
                message = f"Node '{node.id}' ({node.type}) has unknown config field '{field}'"
            else:
                message = f"Node '{node.id}' ({node.type}) config field '{field}': {error['msg']}"
            issues.append(ValidationIssue(
                code=IssueCode.MISSING_CONFIG if missing else IssueCode.INVALID_CONFIG,
                node_id=node.id, field=field, message=message,
            ))
        return issues
    return []


def _reference_problem(
    expression: str,
    node: GraphNode,
    nodes_by_id: dict[str, GraphNode],
    definitions: dict[str, NodeDefinition[Any]],
    ancestors: dict[str, set[str]],
    variable_keys: set[str],
) -> str | None:
    try:
        path = parse_reference(expression)
    except VariableResolutionError as exc:
        return exc.reason

    root, rest = path[0], path[1:]
    if root == "vars":
        if not rest:
            return "name a variable, e.g. {{vars.my_key}}"
        if str(rest[0]) not in variable_keys:
            known = ", ".join(sorted(variable_keys)) or "none defined"
            return f"no workflow variable '{rest[0]}' (variables: {known})"
        return None
    if root == "system":
        if not rest or str(rest[0]) not in SYSTEM_KEYS:
            return f"unknown system value (available: {', '.join(sorted(SYSTEM_KEYS))})"
        return None
    if root not in nodes_by_id:
        return f"'{root}' is not a node in this graph, 'vars', or 'system'"
    if root == node.id:
        return "a node cannot reference its own output"
    if root not in ancestors[node.id]:
        return f"node '{root}' is not upstream of '{node.id}', so its output isn't available yet"

    source_def = definitions.get(root)
    if rest and source_def is not None:
        keys = source_def.output_keys(nodes_by_id[root])
        if keys is not None and str(rest[0]) not in keys:
            return f"node '{root}' ({source_def.type}) has no output '{rest[0]}' (outputs: {', '.join(sorted(keys))})"
    return None


def _duplicate_name_issues(
    nodes: Iterable[GraphNode], definitions: dict[str, NodeDefinition[Any]]
) -> list[ValidationIssue]:
    """Input names key the run's `inputs`, output names key the final output — both must be unique."""
    issues = []
    for node_type, default in (("input", None), ("output", "result")):
        seen: dict[str, str] = {}
        for node in nodes:
            definition = definitions.get(node.id)
            if definition is None or definition.type != node_type:
                continue
            name = node.config.get("name") or default or node.id
            if not isinstance(name, str):
                continue
            if name in seen:
                issues.append(ValidationIssue(
                    code=IssueCode.DUPLICATE_NAME, node_id=node.id, field="name",
                    message=f"{node_type.title()} nodes '{seen[name]}' and '{node.id}' both use the name '{name}'",
                ))
            else:
                seen[name] = node.id
    return issues
