"""Executor hooks, cooperative stop, LLM token streaming, and queue routing."""

import asyncio

import pytest

from flowforge_engine import (
    ExecutionControl,
    ExecutionHooks,
    NodeStatus,
    RunStatus,
    WorkflowGraph,
    execute_graph,
    get_node_definition,
    queue_for_graph,
)
from flowforge_engine.models import NodeResult
from flowforge_engine.providers import MockEmailProvider
from flowforge_engine.registry import NodeConfig, NodeDefinition, NodeRegistry
from flowforge_engine.testing import chain, example_graph, make_context, node


class Recorder(ExecutionHooks):
    def __init__(self):
        self.events = []

    async def node_started(self, node, label, started_at):
        self.events.append(("started", node.id))

    async def node_finished(self, result):
        self.events.append((result.status.value, result.node_id))


class TokenRecorder(Recorder):
    async def node_token(self, node_id, text, provider):
        self.events.append(("token", node_id, text, provider))


def delay_graph(seconds=5):
    return WorkflowGraph(
        nodes=[node("a", "text", text="x"), node("wait", "delay", seconds=seconds), node("out", "output", value="{{a.text}}")],
        edges=chain("a", "wait", "out"),
    )


async def test_hooks_see_every_node_in_order():
    hooks = Recorder()
    result = await execute_graph(example_graph(), make_context(inputs={"topic": "x"}), hooks=hooks)
    assert result.status is RunStatus.SUCCESS
    assert hooks.events == [
        ("started", "input"), ("success", "input"),
        ("started", "gemini"), ("success", "gemini"),
        ("started", "gmail"), ("success", "gmail"),
        ("started", "output"), ("success", "output"),
    ]


async def test_failure_skips_the_rest_and_hooks_report_it():
    graph = example_graph("not-an-email")
    hooks = Recorder()
    result = await execute_graph(graph, make_context(inputs={"topic": "x"}), hooks=hooks)
    assert result.status is RunStatus.FAILED
    assert hooks.events[-3:] == [("started", "gmail"), ("failed", "gmail"), ("skipped", "output")]


async def test_stop_before_start_skips_everything():
    control = ExecutionControl()
    control.request_stop("Stopped by user")
    result = await execute_graph(delay_graph(), make_context(), control=control)
    assert result.status is RunStatus.STOPPED and result.error == "Stopped by user"
    assert {r.status for r in result.node_results} == {NodeStatus.SKIPPED}
    assert result.node_results[0].skip_reason == "Not run: Stopped by user"
    assert result.final_output is None


async def test_stop_interrupts_a_running_delay():
    control = ExecutionControl()
    hooks = Recorder()

    async def stop_soon():
        await asyncio.sleep(0.2)
        control.request_stop("Stopped by user")

    stopper = asyncio.create_task(stop_soon())
    loop = asyncio.get_running_loop()
    start = loop.time()
    result = await execute_graph(delay_graph(seconds=30), make_context(), hooks=hooks, control=control)
    await stopper
    assert loop.time() - start < 2  # didn't wait out the 30 s delay
    assert result.status is RunStatus.STOPPED
    by_id = {r.node_id: r for r in result.node_results}
    assert by_id["a"].status is NodeStatus.SUCCESS
    wait = by_id["wait"]
    assert wait.status is NodeStatus.SKIPPED and wait.started_at is not None
    assert wait.skip_reason.startswith("Interrupted after 0.") and wait.skip_reason.endswith("Stopped by user")
    assert by_id["out"].skip_reason == "Not run: Stopped by user"
    assert hooks.events == [("started", "a"), ("success", "a"), ("started", "wait"), ("skipped", "wait"), ("skipped", "out")]


async def test_non_interruptible_node_finishes_before_the_stop():
    """A Gmail send isn't cut off mid-flight: it completes, then the run stops."""
    registry = NodeRegistry()

    class SlowSendConfig(NodeConfig):
        pass

    class SlowSend(NodeDefinition[SlowSendConfig]):
        category, label, description, icon = "integration", "Slow send", "", "mail"
        config_schema = SlowSendConfig
        interruptible = False

        async def execute(self, context, config):
            await asyncio.sleep(0.3)
            return NodeResult.ok(sent=True)

    registry.add("slow_send", SlowSend)
    registry.add("delay", type(get_node_definition("delay")))
    graph = WorkflowGraph(nodes=[node("send", "slow_send"), node("after", "delay", seconds=0)], edges=chain("send", "after"))
    control = ExecutionControl()
    asyncio.get_running_loop().call_later(0.05, control.request_stop)
    result = await execute_graph(graph, make_context(), registry=registry, control=control)
    by_id = {r.node_id: r for r in result.node_results}
    assert by_id["send"].status is NodeStatus.SUCCESS and by_id["send"].output == {"sent": True}
    assert by_id["after"].status is NodeStatus.SKIPPED
    assert result.status is RunStatus.STOPPED


async def test_gmail_node_is_not_interruptible():
    assert get_node_definition("gmail").interruptible is False
    assert get_node_definition("delay").interruptible is True


async def test_limit_stop_reports_failed():
    control = ExecutionControl()
    asyncio.get_running_loop().call_later(
        0.1, lambda: control.request_stop("Execution exceeded the time limit of 1s", status=RunStatus.FAILED)
    )
    result = await execute_graph(delay_graph(seconds=30), make_context(), control=control)
    assert result.status is RunStatus.FAILED
    assert result.error == "Execution exceeded the time limit of 1s"


async def test_llm_token_streaming_is_forwarded_only_when_enabled():
    graph = WorkflowGraph(nodes=[node("llm", "gemini", user_prompt="Tell me about tides and moons", stream=True)])
    hooks = TokenRecorder()
    result = await execute_graph(graph, make_context(), hooks=hooks)
    tokens = [e for e in hooks.events if e[0] == "token"]
    assert "".join(t[2] for t in tokens) == result.result_for("llm").output["response"]
    assert len(tokens) > 1 and {t[3] for t in tokens} == {"gemini"}

    quiet = TokenRecorder()
    graph.nodes[0].config["stream"] = False
    await execute_graph(graph, make_context(), hooks=quiet)
    assert not [e for e in quiet.events if e[0] == "token"]


async def test_streaming_without_a_token_observer_still_works():
    graph = WorkflowGraph(nodes=[node("llm", "groq", user_prompt="hi", stream=True)])
    result = await execute_graph(graph, make_context(), hooks=Recorder())  # no node_token override
    assert result.status is RunStatus.SUCCESS


def test_queue_routing():
    assert queue_for_graph(example_graph()) == "default"

    registry = NodeRegistry()
    registry.add("text", type(get_node_definition("text")))

    class GpuConfig(NodeConfig):
        pass

    class GpuNode(NodeDefinition[GpuConfig]):
        category, label, description, icon = "ai", "GPU", "", "cpu"
        config_schema = GpuConfig
        queue = "gpu"

        async def execute(self, context, config):  # pragma: no cover
            raise NotImplementedError

    registry.add("gpu", GpuNode)
    graph = WorkflowGraph(nodes=[node("t", "text", text="x"), node("g", "gpu")], edges=chain("t", "g"))
    assert queue_for_graph(graph, registry=registry) == "gpu"
    assert registry.get("gpu").describe()["queue"] == "gpu"


@pytest.fixture(autouse=True)
def _outbox():
    MockEmailProvider.clear_outbox()
