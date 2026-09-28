from __future__ import annotations

import asyncio
import contextlib
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


class ExecutionHooks:
    """Observer for a run. Subclass and override what you need; every method is awaited in
    execution order, and an exception raised here aborts the run (it is not swallowed)."""

    async def node_started(self, node: GraphNode, label: str, started_at: datetime) -> None:
        """A node is about to run (not called for nodes that are skipped outright)."""

    async def node_finished(self, result: NodeRunResult) -> None:
        """A node succeeded, failed, or was skipped (including interrupted by a stop)."""

    async def node_token(self, node_id: str, text: str, provider: str) -> None:
        """A streamed LLM text delta. Only called when this method is overridden."""


def _streams_tokens(hooks: ExecutionHooks | None) -> bool:
    return hooks is not None and type(hooks).node_token is not ExecutionHooks.node_token


class ExecutionControl:
    """Lets another task stop a run cooperatively.

    request_stop() makes the executor skip every node that hasn't started. A node that is
    running is cancelled if its type is `interruptible` (Delay, LLM calls); otherwise it is
    allowed to finish first (Gmail sending). `status` is what the run reports: STOPPED for a
    user stop, or FAILED when the stop enforces a limit (e.g. a time limit).
    """

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self.reason: str | None = None
        self.status: RunStatus = RunStatus.STOPPED

    def request_stop(self, reason: str = "Stopped by user", *, status: RunStatus = RunStatus.STOPPED) -> None:
        if not self._stop.is_set():
            self.reason, self.status = reason, status
            self._stop.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    async def wait(self) -> None:
        await self._stop.wait()


async def execute_graph(
    workflow: WorkflowGraph,
    context: NodeContext,
    *,
    registry: NodeRegistry | None = None,
    node_timeout: float = DEFAULT_NODE_TIMEOUT_SECONDS,
    hooks: ExecutionHooks | None = None,
    control: ExecutionControl | None = None,
) -> ExecutionResult:
    """Run every node in topological order and collect per-node results.

    Each node's config is resolved against the outputs of nodes that already ran, just
    before it executes. The first failure stops the run and the remaining nodes are
    marked skipped. Nodes behind a Condition branch that wasn't taken are skipped too,
    but that is not a failure. A stop via `control` skips everything not yet finished.

    Raises GraphValidationFailed (without running anything) if the graph is invalid,
    including when a node's provider has no credentials ("Authentication missing").
    """
    registry = registry or default_registry
    issues = validate_workflow(workflow, registry=registry, services=context.services)
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
    stopped = False

    for node_id in topological_sort(workflow.nodes, workflow.edges):
        node = nodes[node_id]
        definition = registry.get(node.type)
        assert definition is not None  # guaranteed by validation

        if failed_node is not None:
            result = _skipped(node, definition, f"Execution stopped after node '{failed_node}' failed")
        elif stopped or (control is not None and control.stop_requested):
            stopped = True
            result = _skipped(node, definition, f"Not run: {control.reason if control else 'stopped'}")
        elif skip_reason := _inactive_reason(incoming[node_id], results, registry, nodes):
            result = _skipped(node, definition, skip_reason)
        else:
            result = await _run_node(node, definition, context, node_timeout, hooks, control)
            if result.status is NodeStatus.SUCCESS:
                context.node_outputs[node_id] = result.output or {}
            elif result.status is NodeStatus.SKIPPED:  # interrupted by a stop
                stopped = True
            else:
                failed_node = node_id

        results[node_id] = result
        if hooks is not None:
            await hooks.node_finished(result)

    finished_at = datetime.now(UTC)
    ordered = list(results.values())
    if failed_node:
        status, error = RunStatus.FAILED, f"Node '{failed_node}' failed: {results[failed_node].error}"
    elif stopped:
        assert control is not None
        status, error = control.status, control.reason
    else:
        status, error = RunStatus.SUCCESS, None
    return ExecutionResult(
        workflow_id=context.workflow_id,
        execution_id=context.execution_id,
        status=status,
        node_results=ordered,
        final_output=_final_output(ordered, workflow, registry) if status is RunStatus.SUCCESS else None,
        error=error,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=round((time.perf_counter() - clock) * 1000),
    )


async def execute_node(
    node: GraphNode,
    context: NodeContext,
    *,
    registry: NodeRegistry | None = None,
    node_timeout: float = DEFAULT_NODE_TIMEOUT_SECONDS,
    hooks: ExecutionHooks | None = None,
) -> NodeRunResult:
    """Run a single node in isolation, exactly as execute_graph would run it.

    `context.node_outputs` stands in for upstream results and `context.variables` /
    `context.inputs` for workflow variables and run inputs, so {{...}} references resolve
    against sample data. Nothing else in the graph runs. Raises ValueError for an unknown
    node type; every other problem is reported in the result (status failed, `error`).
    """
    registry = registry or default_registry
    definition = registry.get(node.type)
    if definition is None:
        raise ValueError(f"Unknown node type '{node.type}'")
    return await _run_node(node, definition, context, node_timeout, hooks, None)


class _Interrupted(Exception):
    """The node was cancelled because a stop was requested while it ran."""


async def _run_with_control(coro: Any, definition: NodeDefinition[Any], control: ExecutionControl | None) -> Any:
    """Await the node, cancelling it on a stop request if its type allows that."""
    if control is None or not definition.interruptible:
        return await coro
    node_task = asyncio.ensure_future(coro)
    stop_task = asyncio.ensure_future(control.wait())
    try:
        done, _ = await asyncio.wait({node_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if node_task in done:
            return node_task.result()
        node_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await node_task
        raise _Interrupted
    finally:
        for task in (node_task, stop_task):
            if not task.done():
                task.cancel()


async def _run_node(
    node: GraphNode,
    definition: NodeDefinition[Any],
    context: NodeContext,
    timeout: float,
    hooks: ExecutionHooks | None,
    control: ExecutionControl | None,
) -> NodeRunResult:
    update: dict[str, Any] = {"node_id": node.id, "on_token": None}
    if _streams_tokens(hooks):
        assert hooks is not None

        async def on_token(text: str, provider: str) -> None:
            await hooks.node_token(node.id, text, provider)

        update["on_token"] = on_token
    node_context = context.model_copy(update=update)
    started_at = datetime.now(UTC)
    if hooks is not None:
        await hooks.node_started(node, _label(node, definition), started_at)
    clock = time.perf_counter()
    resolved: dict[str, Any] = node.config
    output: dict[str, Any] | None = None
    error: str | None = None
    interrupted = False

    try:
        resolved = resolve_value(node.config, build_scope(node_context))
        config = definition.config_schema.model_validate(resolved)
        node_result = await _run_with_control(
            asyncio.wait_for(definition.execute(node_context, config), timeout=timeout), definition, control
        )
        output = node_result.output
        if not node_result.success:
            error = node_result.error or "Node reported failure without an error message"
    except _Interrupted:
        interrupted = True
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
    if interrupted:
        status = NodeStatus.SKIPPED
    else:
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
        skip_reason=(
            f"Interrupted after {duration_ms / 1000:.1f}s: {control.reason if control else 'stopped'}"
            if interrupted else None
        ),
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
