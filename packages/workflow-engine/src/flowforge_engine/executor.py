from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import pydantic

from flowforge_engine.errors import GraphValidationFailed, VariableResolutionError
from flowforge_engine.graph import topological_sort, validate_workflow
from flowforge_engine.models import (
    ExecutionResult,
    GraphEdge,
    GraphNode,
    NodeContext,
    NodeRunResult,
    NodeStatus,
    RunStatus,
    WorkflowGraph,
)
from flowforge_engine.registry import NodeDefinition, NodeRegistry, default_registry
from flowforge_engine.variables import build_scope, resolve_value

logger = logging.getLogger(__name__)

DEFAULT_NODE_TIMEOUT_SECONDS = 120.0


async def execute_graph(
    workflow: WorkflowGraph,
    context: NodeContext,
    *,
    registry: NodeRegistry | None = None,
    node_timeout: float = DEFAULT_NODE_TIMEOUT_SECONDS,
) -> ExecutionResult:
    """Run every node in topological order and collect per-node results.

    Each node's config is resolved against the outputs of nodes that already ran, just
    before it executes. The first failure stops the run and the remaining nodes are
    marked skipped. Nodes behind a Condition branch that wasn't taken are skipped too,
    but that is not a failure.

    Raises GraphValidationFailed (without running anything) if the graph is invalid.
    """
    registry = registry or default_registry
    issues = validate_workflow(workflow, registry=registry)
    if issues:
        raise GraphValidationFailed(issues)

    # Graph variables fill in anything the caller didn't set explicitly.
    context.variables = {v.key: v.value for v in workflow.variables} | context.variables

    nodes = {node.id: node for node in workflow.nodes}
    incoming: dict[str, list[GraphEdge]] = defaultdict(list)
    for edge in workflow.edges:
        incoming[edge.target].append(edge)

    started_at = datetime.now(UTC)
    clock = time.perf_counter()
    results: dict[str, NodeRunResult] = {}
    failed_node: str | None = None

    for node_id in topological_sort(workflow.nodes, workflow.edges):
        node = nodes[node_id]
        definition = registry.get(node.type)
        assert definition is not None  # guaranteed by validation

        if failed_node is not None:
            results[node_id] = _skipped(node, definition, f"Execution stopped after node '{failed_node}' failed")
            continue

        skip_reason = _inactive_reason(incoming[node_id], results, registry, nodes)
        if skip_reason:
            results[node_id] = _skipped(node, definition, skip_reason)
            continue

        result = await _run_node(node, definition, context, node_timeout)
        results[node_id] = result
        if result.status is NodeStatus.SUCCESS:
            context.node_outputs[node_id] = result.output or {}
        else:
            failed_node = node_id

    finished_at = datetime.now(UTC)
    ordered = list(results.values())
    status = RunStatus.FAILED if failed_node else RunStatus.SUCCESS
    return ExecutionResult(
        workflow_id=context.workflow_id,
        execution_id=context.execution_id,
        status=status,
        node_results=ordered,
        final_output=None if failed_node else _final_output(ordered, workflow, registry),
        error=f"Node '{failed_node}' failed: {results[failed_node].error}" if failed_node else None,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=round((time.perf_counter() - clock) * 1000),
    )


async def _run_node(
    node: GraphNode,
    definition: NodeDefinition[Any],
    context: NodeContext,
    timeout: float,
) -> NodeRunResult:
    node_context = context.model_copy(update={"node_id": node.id})
    started_at = datetime.now(UTC)
    clock = time.perf_counter()
    resolved: dict[str, Any] = node.config
    output: dict[str, Any] | None = None
    error: str | None = None

    try:
        resolved = resolve_value(node.config, build_scope(node_context))
        config = definition.config_schema.model_validate(resolved)
        node_result = await asyncio.wait_for(definition.execute(node_context, config), timeout=timeout)
        output = node_result.output
        if not node_result.success:
            error = node_result.error or "Node reported failure without an error message"
    except VariableResolutionError as exc:
        error = str(exc)
    except pydantic.ValidationError as exc:
        error = "Invalid config after resolving variables: " + "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'config'}: {e['msg']}" for e in exc.errors()
        )
    except TimeoutError:
        error = f"Timed out after {timeout:g}s"
    except Exception as exc:  # a node bug must not take down the whole run
        logger.exception("node raised", extra={"node_id": node.id, "node_type": node.type})
        error = f"{type(exc).__name__}: {exc}"

    duration_ms = round((time.perf_counter() - clock) * 1000)
    status = NodeStatus.FAILED if error else NodeStatus.SUCCESS
    logger.info(
        "node finished",
        extra={
            "execution_id": context.execution_id,
            "node_id": node.id,
            "node_type": node.type,
            "status": status.value,
            "duration_ms": duration_ms,
        },
    )
    return NodeRunResult(
        node_id=node.id,
        node_type=node.type,
        label=_label(node, definition),
        status=status,
        input=resolved if isinstance(resolved, dict) else node.config,
        output=output,
        error=error,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        duration_ms=duration_ms,
    )


def _inactive_reason(
    edges: list[GraphEdge],
    results: dict[str, NodeRunResult],
    registry: NodeRegistry,
    nodes: dict[str, GraphNode],
) -> str | None:
    """Why a node with incoming edges can't run, or None if at least one edge is live."""
    if not edges:
        return None
    reasons = []
    for edge in edges:
        source = results[edge.source]
        definition = registry.get(nodes[edge.source].type)
        if source.status is not NodeStatus.SUCCESS:
            reasons.append(f"upstream node '{edge.source}' was skipped")
            continue
        if definition and definition.branches:
            taken = (source.output or {}).get("branch")
            if edge.source_handle != taken:
                reasons.append(f"condition '{edge.source}' took the '{taken}' branch")
                continue
        return None  # this edge is live
    return "Not reached: " + "; ".join(reasons)


def _final_output(
    results: list[NodeRunResult], workflow: WorkflowGraph, registry: NodeRegistry
) -> dict[str, Any]:
    """{name: value} from Output nodes; without any, the outputs of the graph's sinks."""
    outputs = {}
    for result in results:
        definition = registry.get(result.node_type)
        if definition and definition.produces_final_output and result.status is NodeStatus.SUCCESS:
            output = result.output or {}
            outputs[output.get("name", result.node_id)] = output.get("value")
    if outputs:
        return outputs

    has_outgoing = {edge.source for edge in workflow.edges}
    return {
        r.node_id: r.output
        for r in results
        if r.status is NodeStatus.SUCCESS and r.node_id not in has_outgoing
    }


def _skipped(node: GraphNode, definition: NodeDefinition[Any], reason: str) -> NodeRunResult:
    return NodeRunResult(
        node_id=node.id,
        node_type=node.type,
        label=_label(node, definition),
        status=NodeStatus.SKIPPED,
        skip_reason=reason,
    )


def _label(node: GraphNode, definition: NodeDefinition[Any]) -> str:
    return node.label or definition.label
