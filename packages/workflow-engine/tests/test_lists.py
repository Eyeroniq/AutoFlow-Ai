"""List nodes: For Each (concurrency, per-minute rate limit, per-item failures, JSON replies,
time budget), Filter, Join, the per-item {{item}} / {{index}} scope, and its validation."""

import asyncio

import pytest

from flowforge_engine import (
    ExecutionServices,
    IssueCode,
    NodeStatus,
    ProviderSettings,
    RunStatus,
    WorkflowGraph,
    execute_graph,
    execute_node,
    validate_workflow,
)
from flowforge_engine.errors import ProviderError
from flowforge_engine.nodes import lists
from flowforge_engine.nodes.lists import RateLimiter, as_list, parse_json_reply
from flowforge_engine.nodes.logic import evaluate
from flowforge_engine.testing import chain, make_context, node


class RecordingLLM:
    """Tracks how many calls are in flight at once; fails prompts containing a marker."""

    is_mock = False

    def __init__(self, name="gemini", *, delay=0.02, fail_on=(), reply=None, clock=None):
        self.name, self.delay, self.fail_on, self.reply, self.clock = name, delay, set(fail_on), reply, clock
        self.inflight = self.max_inflight = 0
        self.prompts: list[str] = []
        self.started_at: list[float] = []

    async def generate(self, system_prompt, user_prompt, model, temperature, max_tokens):
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        if self.clock is not None:
            self.started_at.append(self.clock())
        try:
            await asyncio.sleep(self.delay)
            self.prompts.append(user_prompt)
            if any(marker in user_prompt for marker in self.fail_on):
                raise ProviderError(self.name, "rate limited (HTTP 429): quota exceeded")
            return self.reply(user_prompt) if self.reply else f"summary of {user_prompt}"
        finally:
            self.inflight -= 1


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def services(**llms):
    return ExecutionServices(provider_settings=ProviderSettings(testing=True), llm_providers=llms or None)


async def for_each(items, *, llm=None, context=None, **config):
    config = {"items": items, "prompt": "Summarize: {{item}}", **config}
    ctx = context or make_context(services=services(gemini=llm) if llm else services())
    return await execute_node(node("each", "for_each", **config), ctx)


# --- For Each ----------------------------------------------------------------------------


async def test_llm_mode_answers_every_item_in_order():
    llm = RecordingLLM()
    result = await for_each(["alpha", "beta", "gamma"], llm=llm, prompt="Summarize {{index}}: {{item}}")
    assert result.status is NodeStatus.SUCCESS, result.error
    out = result.output
    assert out["outputs"] == ["summary of Summarize 0: alpha", "summary of Summarize 1: beta", "summary of Summarize 2: gamma"]
    assert [r["index"] for r in out["results"]] == [0, 1, 2]
    assert all(r["ok"] and r["provider_used"] == "gemini" for r in out["results"])
    assert (out["count"], out["succeeded"], out["failed"], out["skipped"]) == (3, 3, 0, 0)
    # The executor keeps the per-item template as written.
    assert result.input["prompt"] == "Summarize {{index}}: {{item}}"


@pytest.mark.parametrize("concurrency", [1, 2, 4])
async def test_concurrency_limit_is_never_exceeded(concurrency):
    llm = RecordingLLM(delay=0.03)
    result = await for_each(list(range(8)), llm=llm, concurrency=concurrency, rate_limit_per_minute=0)
    assert result.status is NodeStatus.SUCCESS
    assert llm.max_inflight == concurrency


async def test_rate_limit_spreads_calls_over_minutes(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(lists, "_now", clock)
    monkeypatch.setattr(lists, "_sleep", clock.sleep)
    llm = RecordingLLM(delay=0, clock=clock)
    result = await for_each(list(range(7)), llm=llm, concurrency=5, rate_limit_per_minute=3)
    assert result.status is NodeStatus.SUCCESS and result.output["succeeded"] == 7
    starts = sorted(llm.started_at)
    # No 60-second window ever holds more than 3 call starts...
    for i, start in enumerate(starts):
        assert sum(1 for s in starts[i:] if s < start + 60) <= 3
    # ...and the calls go out in bursts of 3 at t=0, 60, 120 (not one by one).
    assert starts == [0, 0, 0, 60, 60, 60, 120]
    assert result.output["rate_limited_seconds"] == 120


async def test_rate_limiter_unit():
    clock = FakeClock()
    limiter = RateLimiter(2, clock=clock, sleep=clock.sleep)
    assert [await limiter.acquire() for _ in range(3)] == [True, True, True]
    assert clock.sleeps == [60] and clock.now == 60
    # It won't wait past a deadline.
    assert await limiter.acquire(deadline=clock.now + 10) is True  # t=60: the window holds one start
    assert await limiter.acquire(deadline=clock.now + 10) is False
    assert await RateLimiter(0).acquire() is True  # 0 = unlimited


async def test_partial_failures_are_recorded_per_item():
    llm = RecordingLLM(fail_on={"beta"})
    result = await for_each(["alpha", "beta", "gamma"], llm=llm)
    assert result.status is NodeStatus.SUCCESS
    out = result.output
    failed = out["results"][1]
    assert failed["ok"] is False and failed["output"] is None and "429" in failed["error"]
    assert failed["item"] == "beta"
    assert out["outputs"] == ["summary of Summarize: alpha", "summary of Summarize: gamma"]
    assert (out["succeeded"], out["failed"]) == (2, 1)


async def test_fail_when_controls_node_failure():
    llm = RecordingLLM(fail_on={"beta"})
    strict = await for_each(["alpha", "beta"], llm=llm, fail_when="any_failed")
    assert strict.status is NodeStatus.FAILED and "1 of 2 items failed" in strict.error
    assert strict.output["succeeded"] == 1  # the results are still there

    all_bad = await for_each(["beta", "beta!"], llm=RecordingLLM(fail_on={"beta"}))
    assert all_bad.status is NodeStatus.FAILED and all_bad.error.startswith("All 2 items failed")

    lenient = await for_each(["beta"], llm=RecordingLLM(fail_on={"beta"}), fail_when="never")
    assert lenient.status is NodeStatus.SUCCESS and lenient.output["failed"] == 1


async def test_json_output_is_parsed_per_item():
    replies = {"good": '```json\n{"score": 82, "reason": "Python"}\n```', "prose": 'Sure! {"score": 40}', "bad": "no json here"}
    llm = RecordingLLM(reply=lambda prompt: next(v for k, v in replies.items() if k in prompt))
    result = await for_each(["good", "prose", "bad"], llm=llm, output_format="json", prompt="Score {{item}}")
    out = result.output
    assert out["results"][0]["output"] == {"score": 82, "reason": "Python"}
    assert out["results"][1]["output"] == {"score": 40}
    assert out["results"][2]["ok"] is False and "not valid JSON" in out["results"][2]["error"]


async def test_item_fields_objects_and_variables_in_the_prompt():
    llm = RecordingLLM()
    ctx = make_context(services=services(gemini=llm), variables={"tone": "brief"})
    items = [{"title": "Rust 2.0", "link": "https://x.test/1"}, {"title": "Python 4", "link": "https://x.test/2"}]
    result = await for_each(items, context=ctx, prompt="{{vars.tone}}: {{item.title}}", system_prompt="Item {{index}}")
    assert result.status is NodeStatus.SUCCESS
    assert llm.prompts == ["brief: Rust 2.0", "brief: Python 4"]
    # A whole object goes in as JSON.
    whole = await for_each([{"a": 1}], llm=RecordingLLM(), prompt="{{item}}")
    assert whole.output["outputs"] == ['summary of {"a": 1}']


async def test_template_mode_calls_no_llm_and_limits_and_flattens():
    result = await for_each(
        [["a", "b"], ["c"], "d"], mode="template", prompt="#{{index}} {{item}}", flatten=True, max_items=3
    )
    assert result.status is NodeStatus.SUCCESS
    assert result.output["outputs"] == ["#0 a", "#1 b", "#2 c"]
    assert result.output["skipped"] == 1 and result.output["mode"] == "template"


async def test_missing_item_field_fails_that_item_only():
    result = await for_each([{"title": "x"}, {"name": "y"}], mode="template", prompt="{{item.title}}")
    first, second = result.output["results"]
    assert first["ok"] and first["output"] == "x"
    assert not second["ok"] and "key 'title' not found" in second["error"]


async def test_time_budget_marks_unstarted_items_not_run():
    ctx = make_context(services=services(gemini=RecordingLLM()))
    # The deadline comes from timeout_seconds: 1s is less than the safety margin, so
    # nothing may start.
    result = await execute_node(
        node("each", "for_each", items=["a", "b"], prompt="{{item}}", fail_when="never", timeout_seconds=1), ctx
    )
    assert result.status is NodeStatus.SUCCESS
    assert all("time budget" in r["error"] for r in result.output["results"])


async def test_items_must_be_a_list():
    result = await for_each({"emails": [], "count": 0}, mode="template")
    assert result.status is NodeStatus.FAILED and "did you mean one of its lists: emails" in result.error
    assert as_list('["x", 1]') == ["x", 1] and as_list(None) == []
    with pytest.raises(ValueError, match="must be a list"):
        as_list(42)


def test_parse_json_reply():
    assert parse_json_reply('[1, 2]') == [1, 2]
    assert parse_json_reply('Result:\n{"a": {"b": 1}}\nThanks') == {"a": {"b": 1}}
    with pytest.raises(ValueError):
        parse_json_reply("nothing")


# --- Filter --------------------------------------------------------------------------------


async def run_filter(items, **config):
    return await execute_node(node("keep", "filter", items=items, **config), make_context())


async def test_filter_numeric_threshold_on_a_nested_field():
    items = [{"output": {"score": 85}}, {"output": {"score": "70"}}, {"output": {"score": 12}}, {"output": None}, {"x": 1}]
    result = await run_filter(items, field="output.score", operator="greater_or_equal", value="70")
    assert result.status is NodeStatus.SUCCESS
    assert result.output["items"] == items[:2]
    assert result.output["count"] == 2 and result.output["removed"] == 3


async def test_filter_text_operators_and_item_prefix():
    emails = [{"subject": "URGENT: server down"}, {"subject": "Lunch?"}, {"subject": None}]
    urgent = await run_filter(emails, field="item.subject", operator="contains", value="urgent")
    assert urgent.output["items"] == [emails[0]]  # case-insensitive by default
    strict = await run_filter(emails, field="subject", operator="contains", value="urgent", case_sensitive=True)
    assert strict.output["items"] == []
    empty = await run_filter(emails + [{}], field="subject", operator="is_empty")
    assert empty.output["items"] == [{"subject": None}, {}]
    regex = await run_filter(["a1", "b2", "c"], operator="matches", value=r"\d$")
    assert regex.output["items"] == ["a1", "b2"]


async def test_filter_records_items_it_cannot_compare():
    result = await run_filter([{"s": "high"}, {"s": 9}], field="s", operator="greater_than", value=5)
    assert result.output["items"] == [{"s": 9}]
    assert result.output["errors"] == [{"index": 0, "error": "'greater_than' needs two numbers, got 'high' and 5"}]


def test_new_condition_operators():
    assert evaluate(" Urgent ", "equals", "urgent", case_sensitive=False)
    assert not evaluate(" Urgent ", "equals", "urgent")
    assert evaluate(70, "greater_or_equal", "70") and evaluate(3, "less_or_equal", 3.0)
    assert evaluate("spam", "not_contains", "urgent") and evaluate([], "is_empty") and evaluate("x", "is_not_empty")
    assert evaluate("Invoice 12", "starts_with", "invoice", case_sensitive=False) and evaluate("a.pdf", "ends_with", ".pdf")
    with pytest.raises(ValueError, match="invalid regular expression"):
        evaluate("x", "matches", "(")


# --- Join ----------------------------------------------------------------------------------


async def run_join(items, **config):
    return await execute_node(node("join", "join", items=items, **config), make_context(variables={"sep": " | "}))


async def test_join_formats_each_item():
    items = [{"title": "A", "link": "https://a.test"}, {"title": "B", "link": "https://b.test"}]
    result = await run_join(items, template="{{item.title}} ({{item.link}})", numbered=True, header="Today:", footer="-- end")
    assert result.status is NodeStatus.SUCCESS
    assert result.output["text"] == "Today:\n1. A (https://a.test)\n2. B (https://b.test)\n-- end"
    assert result.output["count"] == 2


async def test_join_separator_escapes_blank_template_and_empty_text():
    assert (await run_join(["x", {"k": 1}], separator="\\n\\n")).output["text"] == 'x\n\n{"k": 1}'
    assert (await run_join(["x", "y"], separator="{{vars.sep}}")).output["text"] == "x | y"
    empty = await run_join([], empty_text="Nothing new today.", header="ignored")
    assert empty.output == {"text": "Nothing new today.", "count": 0, "truncated": False}
    capped = await run_join(["abc", "def"], max_items=1)
    assert capped.output["text"] == "abc" and capped.output["truncated"] is True


# --- the per-item scope in graphs ---------------------------------------------------------


def list_graph(prompt="{{item.title}}", items="{{feed.value}}", join_template="- {{item}}", extra=None):
    nodes = [
        node("feed", "input", name="feed", input_type="json"),
        node("each", "for_each", items=items, prompt=prompt, mode="template"),
        node("join", "join", items="{{each.outputs}}", template=join_template),
        node("out", "output", value="{{join.text}}"),
    ]
    return WorkflowGraph(nodes=nodes + (extra or []), edges=chain("feed", "each", "join", "out"))


def reference_issues(graph):
    return [i for i in validate_workflow(graph) if i.code is IssueCode.UNRESOLVABLE_REFERENCE]


def test_item_references_are_valid_only_in_per_item_fields():
    assert reference_issues(list_graph()) == []
    bad_items = reference_issues(list_graph(items="{{item}}"))
    assert len(bad_items) == 1 and "only available in a list node's per-item template" in bad_items[0].message
    bad_index = reference_issues(list_graph(prompt="{{index.x}}"))
    assert "{{index}} is a number" in bad_index[0].message
    # Other references in a per-item field are still checked.
    unknown = reference_issues(list_graph(prompt="{{ghost.x}} {{item}}"))
    assert len(unknown) == 1 and "'ghost' is not a node" in unknown[0].message
    outside = reference_issues(list_graph(extra=[node("t", "text", text="{{item.title}}")]))
    assert len(outside) == 1 and outside[0].node_id == "t"


async def test_for_each_and_join_run_in_a_graph():
    feed = [{"title": "One"}, {"title": "Two"}]
    ctx = make_context(inputs={"feed": feed})
    result = await execute_graph(list_graph(), ctx)
    assert result.status is RunStatus.SUCCESS, result.error
    assert result.final_output == {"result": "- One\n- Two"}
