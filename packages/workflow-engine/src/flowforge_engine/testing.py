"""Helpers for tests that build graphs or run nodes (used by the engine and API suites)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from flowforge_engine.models import GraphEdge, GraphNode, GraphVariable, NodeContext, WorkflowGraph
from flowforge_engine.providers.mock import MockEmailProvider, MockLLMProvider
from flowforge_engine.services import ExecutionServices


def mock_services(*, http_transport: httpx.AsyncBaseTransport | None = None) -> ExecutionServices:
    """Services wired to mock providers regardless of which API keys are configured."""
    return ExecutionServices(
        llm_providers={name: MockLLMProvider(name) for name in ("gemini", "openai", "anthropic")},
        email_providers={"gmail": MockEmailProvider("gmail")},
        http_transport=http_transport,
    )


def make_context(
    *,
    inputs: dict[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
    node_outputs: dict[str, dict[str, Any]] | None = None,
    node_id: str | None = None,
    services: ExecutionServices | None = None,
) -> NodeContext:
    return NodeContext(
        workflow_id=str(uuid.uuid4()),
        execution_id=str(uuid.uuid4()),
        node_id=node_id,
        inputs=inputs or {},
        variables=variables or {},
        node_outputs=node_outputs or {},
        services=services or mock_services(),
    )


def node(node_id: str, node_type: str, label: str | None = None, **config: Any) -> GraphNode:
    return GraphNode(id=node_id, type=node_type, label=label, config=config)


def edge(source: str, target: str, source_handle: str | None = None) -> GraphEdge:
    return GraphEdge(source=source, target=target, source_handle=source_handle)


def chain(*node_ids: str) -> list[GraphEdge]:
    return [edge(a, b) for a, b in zip(node_ids, node_ids[1:], strict=False)]


def example_graph(recipient: str = "demo@flowforge.ai") -> WorkflowGraph:
    """Input → Gemini → Gmail → Output: the canonical Phase 2 example."""
    return WorkflowGraph(
        nodes=[
            node("input", "input", "Topic", name="topic", input_type="text",
                 default="the history of workflow automation"),
            node("gemini", "gemini", "Summarize",
                 system_prompt="You are a concise technical writer.",
                 user_prompt="Write a three-sentence summary of {{input.topic}}.",
                 temperature=0.4),
            node("gmail", "gmail", "Email summary",
                 to="{{vars.recipient}}", subject="Summary: {{input.topic}}", body="{{gemini.response}}"),
            node("output", "output", "Result",
                 name="result", value={"summary": "{{gemini.response}}", "email": "{{gmail}}"}),
        ],
        edges=chain("input", "gemini", "gmail", "output"),
        variables=[GraphVariable(key="recipient", value=recipient)],
    )
