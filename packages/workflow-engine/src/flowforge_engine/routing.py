"""Which task queue runs which node, and where a run starts.

A run moves between queues: a worker runs consecutive nodes of the queues it consumes,
then hands the run off to the queue of the next node (flowforge_engine.executor,
`accepts`). Each kind of work scales on its own workers:

- "llm": model calls (I/O-bound, rate-limited): many concurrent slots per worker.
- "ocr": document processing (CPU-bound): a few slots per worker, more worker containers.
- "default": everything else (HTTP, email, delays).

Portable nodes (Input, Output, Text, Condition) run wherever the run currently is.
"""

from __future__ import annotations

from flowforge_engine.graph import topological_sort
from flowforge_engine.models import GraphNode, WorkflowGraph
from flowforge_engine.registry import NodeRegistry, default_registry

DEFAULT_QUEUE = "default"
LLM_QUEUE = "llm"
OCR_QUEUE = "ocr"
QUEUES = (DEFAULT_QUEUE, LLM_QUEUE, OCR_QUEUE)


def queue_for_node(node: GraphNode, *, registry: NodeRegistry | None = None) -> str | None:
    """The queue `node` must run on, or None if it's portable (or of an unknown type)."""
    definition = (registry or default_registry).get(node.type)
    if definition is None or definition.portable:
        return None
    return definition.queue


def queue_for_graph(graph: WorkflowGraph, *, registry: NodeRegistry | None = None) -> str:
    """The queue a run of `graph` starts on: that of its first non-portable node in
    execution order, so leading Input nodes don't cost a hand-off. "default" if every node
    is portable."""
    by_id = {node.id: node for node in graph.nodes}
    try:
        order = topological_sort(graph.nodes, graph.edges)
    except Exception:  # an invalid graph never gets enqueued; declaration order is fine here
        order = list(by_id)
    for node_id in order:
        queue = queue_for_node(by_id[node_id], registry=registry)
        if queue is not None:
            return queue
    return DEFAULT_QUEUE
