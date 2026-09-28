"""Helpers for tests that build graphs or run nodes (used by the engine and API suites)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

from flowforge_engine.files import FileStore
from flowforge_engine.models import GraphEdge, GraphNode, GraphVariable, NodeContext, WorkflowGraph
from flowforge_engine.providers.settings import ProviderSettings
from flowforge_engine.services import ExecutionServices


def mock_services(
    *, http_transport: httpx.AsyncBaseTransport | None = None, files: FileStore | None = None
) -> ExecutionServices:
    """Services that hand out mock providers for everything, whatever keys are configured."""
    return ExecutionServices(provider_settings=ProviderSettings(testing=True), http_transport=http_transport, files=files)


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


def example_graph(recipient: str = "you@example.com") -> WorkflowGraph:
    """Input → Gemini → Gmail → Output: the canonical example.

    The default recipient is a reserved example.com address: with real SMTP, set it to your own.
    """
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


# --- live (real-network) tests --------------------------------------------------------


def live_tests_enabled(markexpr: str | None) -> bool:
    """Live tests run only when selected explicitly: `pytest -m live` (or FLOWFORGE_LIVE_TESTS=1).

    They call real APIs and one of them sends a real email, so a plain `pytest` skips them.
    """
    expr = markexpr or ""
    return os.environ.get("FLOWFORGE_LIVE_TESTS") == "1" or ("live" in expr and "not live" not in expr)


def _dotenv_candidates() -> Iterable[Path]:
    seen: set[Path] = set()
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for directory in (start, *start.parents):
            path = directory / ".env"
            if path not in seen:
                seen.add(path)
                yield path


def _parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key.strip()] = value  # later lines win, as with python-dotenv
    return values


def live_env() -> dict[str, str]:
    """Real configuration for live tests: the process environment over the nearest .env.

    Values are never printed; tests only report which variable is missing.
    """
    merged: dict[str, str] = {}
    for path in _dotenv_candidates():
        if path.is_file():
            merged = _parse_dotenv(path)
            break
    merged.update({k: v for k, v in os.environ.items() if v.strip()})
    return merged


def live_settings(env: dict[str, str] | None = None) -> ProviderSettings:
    """ProviderSettings built from live_env() with mocks disabled (TESTING ignored)."""
    settings = ProviderSettings.from_mapping(env if env is not None else live_env())
    return settings.model_copy(update={"testing": False})
