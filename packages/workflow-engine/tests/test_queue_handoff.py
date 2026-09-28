"""Queue routing and hand-off: which queue each node runs on, where a run starts, and
pausing/resuming a run across workers of different queues."""

from flowforge_engine import (
    ExecutionHooks,
    NodeStatus,
    RunStatus,
    WorkflowGraph,
    default_registry,
    execute_graph,
    queue_for_graph,
    queue_for_node,
)
from flowforge_engine.testing import chain, edge, example_graph, make_context, node


class Recorder(ExecutionHooks):
    def __init__(self):
        self.events = []

    async def node_started(self, node, label, started_at):
        self.events.append(("started", node.id))

    async def node_finished(self, result):
        self.events.append((result.status.value, result.node_id))


def mixed_graph() -> WorkflowGraph:
    """Input (portable) -> Gemini (llm) -> Delay (default) -> Output (portable)."""
    return WorkflowGraph(
        nodes=[
            node("input", "input", name="topic"),
            node("llm", "gemini", provider="mock", user_prompt="About {{input.topic}}"),
            node("wait", "delay", seconds=0),
            node("out", "output", value={"answer": "{{llm.response}}", "waited": "{{wait.waited_seconds}}"}),
        ],
        edges=chain("input", "llm", "wait", "out"),
    )


def test_every_node_type_declares_its_queue():
    queues = {d.type: (d.queue, d.portable) for d in default_registry.definitions()}
    for llm in ("gemini", "groq", "openrouter", "ollama", "openai", "anthropic", "summarize", "extract_entities"):
        assert queues[llm] == ("llm", False), llm
    assert queues["ocr"] == ("ocr", False) and queues["pdf_extract"] == ("ocr", False)
    for general in ("http_request", "gmail", "gmail_read", "delay"):
        assert queues[general] == ("default", False), general
    for portable in ("input", "output", "text", "condition"):
        assert queues[portable][1] is True, portable
    assert queue_for_node(node("x", "input")) is None
    assert queue_for_node(node("x", "ocr")) == "ocr"


def test_a_run_starts_on_the_queue_of_its_first_real_node():
    assert queue_for_graph(example_graph()) == "llm"  # Input is portable; Gemini comes next
    graph = WorkflowGraph(nodes=[node("i", "input"), node("o", "ocr", file="{{i.value}}")], edges=chain("i", "o"))
    assert queue_for_graph(graph) == "ocr"
    only_portable = WorkflowGraph(nodes=[node("t", "text", text="x"), node("o", "output", value="{{t.text}}")],
                                  edges=chain("t", "o"))
    assert queue_for_graph(only_portable) == "default"


async def test_a_worker_hands_off_at_the_first_node_it_does_not_serve():
    hooks = Recorder()
    first = await execute_graph(mixed_graph(), make_context(inputs={"topic": "tides"}), hooks=hooks,
                                accepts={"llm"}.__contains__)
    assert first.status is RunStatus.HANDOFF and first.next_queue == "default"
    assert first.final_output is None and first.error is None
    # Input (portable) and Gemini ran here; Delay and Output didn't.
    assert hooks.events == [("started", "input"), ("success", "input"), ("started", "llm"), ("success", "llm")]
    assert [r.node_id for r in first.node_results] == ["input", "llm"]


async def test_the_next_worker_resumes_with_the_finished_results():
    graph = mixed_graph()
    first = await execute_graph(graph, make_context(inputs={"topic": "tides"}), accepts={"llm"}.__contains__)
    completed = {r.node_id: r for r in first.node_results}

    hooks = Recorder()
    second = await execute_graph(graph, make_context(inputs={"topic": "tides"}), hooks=hooks,
                                 completed=completed, accepts={"default"}.__contains__)
    assert second.status is RunStatus.SUCCESS
    # Finished nodes aren't run (or reported) again; their outputs still resolve downstream.
    assert hooks.events == [("started", "wait"), ("success", "wait"), ("started", "out"), ("success", "out")]
    answer = second.final_output["result"]["answer"]
    assert answer == completed["llm"].output["response"] and "tides" in answer
    assert [r.node_id for r in second.node_results] == ["input", "llm", "wait", "out"]


async def test_no_accepts_means_the_whole_run_happens_here():
    result = await execute_graph(mixed_graph(), make_context(inputs={"topic": "x"}))
    assert result.status is RunStatus.SUCCESS and result.next_queue is None


async def test_skipped_nodes_never_cause_a_hand_off():
    # The LLM node sits behind the untaken branch, so a default-only worker finishes the run.
    graph = WorkflowGraph(
        nodes=[
            node("check", "condition", left="a", operator="equals", right="b"),
            node("llm", "gemini", provider="mock", user_prompt="hi"),
            node("done", "text", text="no LLM needed"),
        ],
        edges=[edge("check", "llm", "true"), edge("check", "done", "false")],
    )
    result = await execute_graph(graph, make_context(), accepts={"default"}.__contains__)
    assert result.status is RunStatus.SUCCESS
    assert result.result_for("llm").status is NodeStatus.SKIPPED


async def test_a_failure_recorded_in_an_earlier_segment_fails_the_run():
    graph = mixed_graph()
    first = await execute_graph(graph, make_context(inputs={"topic": "x"}), accepts={"llm"}.__contains__)
    completed = {r.node_id: r for r in first.node_results}
    completed["llm"] = completed["llm"].model_copy(update={"status": NodeStatus.FAILED, "error": "boom"})
    result = await execute_graph(graph, make_context(inputs={"topic": "x"}), completed=completed)
    assert result.status is RunStatus.FAILED and "boom" in result.error
    assert result.result_for("wait").status is NodeStatus.SKIPPED
