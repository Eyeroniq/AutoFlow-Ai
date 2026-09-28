"""Real outbound HTTP through HTTPRequestNode. Deselect offline with: pytest -m "not network"."""

import pytest

from flowforge_engine import RunStatus, WorkflowGraph, execute_graph
from flowforge_engine.models import GraphVariable
from flowforge_engine.testing import chain, edge, make_context, node

pytestmark = pytest.mark.network


async def test_graph_calls_httpbin_for_real():
    graph = WorkflowGraph(
        nodes=[
            node("topic", "input", name="topic"),
            node("post", "http_request", method="POST", url="https://httpbin.org/anything/flowforge",
                 query={"run": "{{system.execution_id}}"},
                 headers={"X-FlowForge-Test": "{{vars.marker}}"},
                 body={"topic": "{{topic.value}}", "tags": ["a", "b"]}, timeout_seconds=20),
            node("check", "condition", left="{{post.status_code}}", operator="equals", right=200),
            node("out", "output", value={
                "echoed_topic": "{{post.body.json.topic}}",
                "echoed_header": "{{post.body.headers.X-Flowforge-Test}}",
                "run": "{{post.body.args.run}}",
            }),
        ],
        edges=[*chain("topic", "post", "check"), edge("check", "out", "true")],
        variables=[GraphVariable(key="marker", value="phase-2")],
    )
    context = make_context(inputs={"topic": "real network call"})

    result = await execute_graph(graph, context)

    assert result.status is RunStatus.SUCCESS, result.error
    post = result.result_for("post")
    assert post.output["status_code"] == 200
    assert post.output["url"].startswith("https://httpbin.org/anything/flowforge?run=")
    assert result.final_output["result"] == {
        "echoed_topic": "real network call",
        "echoed_header": "phase-2",
        "run": context.execution_id,
    }
