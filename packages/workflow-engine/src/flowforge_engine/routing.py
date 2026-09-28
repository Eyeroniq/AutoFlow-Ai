"""Which task queue a run belongs on, from its nodes' `queue` attributes."""

from __future__ import annotations

from flowforge_engine.graph import topological_sort
from flowforge_engine.models import WorkflowGraph
from flowforge_engine.registry import NodeRegistry, default_registry

DEFAULT_QUEUE = "default"


def queue_for_graph(graph: WorkflowGraph, *, registry: NodeRegistry | None = None) -> str:
    """The queue a run of `graph` is routed to.

    A run executes as one task, so it goes to the first non-default queue any of its nodes
    asks for (in execution order), or "default" when none does. Every built-in node uses
    "default"; a node type that needs special workers (GPU, long-running) sets `queue` and
    gets them via `celery worker -Q <queue>` without touching the dispatch code.
    """
    registry = registry or default_registry
    by_id = {node.id: node for node in graph.nodes}
    try:
        order = topological_sort(graph.nodes, graph.edges)
    except Exception:  # an invalid graph never gets enqueued; declaration order is fine here
        order = list(by_id)
    for node_id in order:
        definition = registry.get(by_id[node_id].type)
        if definition is not None and definition.queue != DEFAULT_QUEUE:
            return definition.queue
    return DEFAULT_QUEUE
