import httpx
import pytest

from flowforge_engine import GraphValidationFailed, NodeStatus, RunStatus, WorkflowGraph, execute_graph
from flowforge_engine.models import GraphVariable
from flowforge_engine.providers import MockEmailProvider
from flowforge_engine.testing import chain, edge, example_graph, make_context, mock_services, node


def statuses(result):
    return {r.node_id: r.status for r in result.node_results}


async def test_example_workflow_end_to_end():
    result = await execute_graph(example_graph(), make_context(inputs={"topic": "solar power"}))

    assert result.status is RunStatus.SUCCESS
    assert result.error is None
    assert [r.node_id for r in result.node_results] == ["input", "gemini", "gmail", "output"]
    assert set(statuses(result).values()) == {NodeStatus.SUCCESS}

    gemini = result.result_for("gemini")
    assert gemini.input["user_prompt"] == "Write a three-sentence summary of solar power."
    assert gemini.output["response"] == "[MOCK RESPONSE to: Write a three-sentence summary of solar power.]"

    gmail = result.result_for("gmail")
    assert gmail.input["body"] == gemini.output["response"]
    assert gmail.input["to"] == "demo@flowforge.ai"
    assert gmail.output["status"] == "sent"

    final = result.final_output["result"]
    assert final["summary"] == gemini.output["response"]
    assert final["email"]["message_id"] == gmail.output["message_id"]
    assert MockEmailProvider.outbox()[0].subject == "Summary: solar power"

    for r in result.node_results:
        assert r.started_at is not None and r.duration_ms is not None and r.duration_ms >= 0


async def test_input_default_used_when_not_supplied():
    result = await execute_graph(example_graph(), make_context())
    assert result.result_for("input").output["topic"] == "the history of workflow automation"


async def test_failure_stops_run_and_skips_the_rest():
    graph = example_graph()
    graph.variables = [GraphVariable(key="recipient", value="not-an-email")]

    result = await execute_graph(graph, make_context())

    assert result.status is RunStatus.FAILED
    assert statuses(result) == {
        "input": NodeStatus.SUCCESS,
        "gemini": NodeStatus.SUCCESS,
        "gmail": NodeStatus.FAILED,
        "output": NodeStatus.SKIPPED,
    }
    assert result.error == "Node 'gmail' failed: Invalid email address(es): not-an-email"
    assert result.result_for("output").skip_reason == "Execution stopped after node 'gmail' failed"
    assert result.final_output is None


async def test_condition_routes_to_one_branch():
    graph = WorkflowGraph(
        nodes=[
            node("amount", "input", input_type="number"),
            node("check", "condition", left="{{amount.value}}", operator="greater_than", right=100),
            node("big", "text", text="big order of {{amount.value}}"),
            node("small", "text", text="small order"),
            node("notify", "text", text="done"),
            node("out", "output", value="{{check.branch}}"),
        ],
        edges=[
            edge("amount", "check"),
            edge("check", "big", "true"),
            edge("check", "small", "false"),
            edge("big", "notify"),
            edge("small", "notify"),
            edge("notify", "out"),
        ],
    )

    big = await execute_graph(graph, make_context(inputs={"amount": "250"}))
    assert big.status is RunStatus.SUCCESS
    assert statuses(big)["big"] is NodeStatus.SUCCESS
    assert statuses(big)["small"] is NodeStatus.SKIPPED
    assert big.result_for("small").skip_reason == "Not reached: condition 'check' took the 'true' branch"
    # A join node runs as long as one incoming path is live.
    assert statuses(big)["notify"] is NodeStatus.SUCCESS
    assert big.result_for("big").output["text"] == "big order of 250"
    assert big.final_output == {"result": "true"}

    small = await execute_graph(graph, make_context(inputs={"amount": 5}))
    assert statuses(small)["big"] is NodeStatus.SKIPPED
    assert statuses(small)["small"] is NodeStatus.SUCCESS
    assert small.final_output == {"result": "false"}


async def test_nodes_behind_untaken_branch_are_skipped_transitively():
    graph = WorkflowGraph(
        nodes=[
            node("check", "condition", left=1, operator="equals", right=2),
            node("a", "text", text="a"),
            node("b", "text", text="b"),
        ],
        edges=[edge("check", "a", "true"), edge("a", "b")],
    )
    result = await execute_graph(graph, make_context())
    assert result.status is RunStatus.SUCCESS
    assert statuses(result) == {"check": NodeStatus.SUCCESS, "a": NodeStatus.SKIPPED, "b": NodeStatus.SKIPPED}
    assert result.result_for("b").skip_reason == "Not reached: upstream node 'a' was skipped"


async def test_invalid_graph_is_rejected_before_running():
    graph = WorkflowGraph(nodes=[node("x", "teleport")])
    with pytest.raises(GraphValidationFailed) as exc:
        await execute_graph(graph, make_context())
    assert exc.value.issues[0].code == "unknown_node_type"


async def test_runtime_resolution_error_fails_the_node():
    # Statically valid (http output has "body"), but the key inside body is missing at run time.
    graph = WorkflowGraph(
        nodes=[
            node("http", "http_request", url="https://api.example.com"),
            node("t", "text", text="id={{http.body.id}}"),
        ],
        edges=chain("http", "t"),
    )
    services = mock_services(http_transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"name": "x"})))
    result = await execute_graph(graph, make_context(services=services))
    assert result.status is RunStatus.FAILED
    assert "key 'id' not found in http.body (available: name)" in result.result_for("t").error


async def test_config_invalid_after_resolution():
    graph = WorkflowGraph(
        nodes=[node("d", "delay", seconds="{{vars.wait}}")],
        variables=[GraphVariable(key="wait", value="forever")],
    )
    result = await execute_graph(graph, make_context())
    assert result.status is RunStatus.FAILED
    assert result.result_for("d").error.startswith("Invalid config after resolving variables: seconds:")


async def test_node_timeout():
    graph = WorkflowGraph(nodes=[node("d", "delay", seconds=1)])
    result = await execute_graph(graph, make_context(), node_timeout=0.05)
    assert result.result_for("d").error == "Timed out after 0.05s"


async def test_unexpected_exception_is_captured():
    from flowforge_engine import NodeConfig, NodeDefinition, NodeRegistry

    registry = NodeRegistry()

    @registry.register("boom")
    class BoomNode(NodeDefinition[NodeConfig]):
        category, label, description, icon = "test", "Boom", "raises", "bomb"
        config_schema = NodeConfig

        async def execute(self, context, config):
            raise RuntimeError("kaboom")

    result = await execute_graph(WorkflowGraph(nodes=[node("b", "boom")]), make_context(), registry=registry)
    assert result.result_for("b").error == "RuntimeError: kaboom"


async def test_final_output_without_output_nodes_uses_sinks():
    graph = WorkflowGraph(
        nodes=[node("a", "text", text="first"), node("b", "text", text="{{a.text}} then second")],
        edges=chain("a", "b"),
    )
    result = await execute_graph(graph, make_context())
    assert result.final_output == {"b": {"text": "first then second"}}


async def test_variables_and_system_values():
    graph = WorkflowGraph(
        nodes=[node("t", "text", text="{{vars.greeting}} from {{system.workflow_id}}")],
        variables=[GraphVariable(key="greeting", value="hello")],
    )
    context = make_context()
    result = await execute_graph(graph, context)
    assert result.result_for("t").output["text"] == f"hello from {context.workflow_id}"
