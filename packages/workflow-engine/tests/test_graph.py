import pytest

from flowforge_engine import CycleError, GraphError, IssueCode, topological_sort, validate_graph, validate_workflow
from flowforge_engine.models import GraphVariable
from flowforge_engine.testing import chain, edge, example_graph, node


def codes(issues):
    return [issue.code for issue in issues]


class TestTopologicalSort:
    def test_linear_chain(self):
        nodes = [node("c", "text", text="c"), node("a", "text", text="a"), node("b", "text", text="b")]
        assert topological_sort(nodes, chain("a", "b", "c")) == ["a", "b", "c"]

    def test_diamond_is_deterministic(self):
        nodes = [node(n, "text", text=n) for n in ("start", "left", "right", "end")]
        edges = [edge("start", "left"), edge("start", "right"), edge("left", "end"), edge("right", "end")]
        assert topological_sort(nodes, edges) == ["start", "left", "right", "end"]
        # Ties follow declaration order.
        nodes_swapped = [nodes[0], nodes[2], nodes[1], nodes[3]]
        assert topological_sort(nodes_swapped, edges) == ["start", "right", "left", "end"]

    def test_disconnected_nodes_keep_declaration_order(self):
        nodes = [node("x", "text", text="x"), node("y", "text", text="y")]
        assert topological_sort(nodes, []) == ["x", "y"]

    def test_two_node_cycle(self):
        nodes = [node("a", "text", text="a"), node("b", "text", text="b")]
        with pytest.raises(CycleError) as exc:
            topological_sort(nodes, [edge("a", "b"), edge("b", "a")])
        assert exc.value.cycle in (["a", "b", "a"], ["b", "a", "b"])

    def test_cycle_behind_an_acyclic_prefix(self):
        nodes = [node(n, "text", text=n) for n in ("start", "a", "b", "c")]
        edges = [edge("start", "a"), edge("a", "b"), edge("b", "c"), edge("c", "a")]
        with pytest.raises(CycleError) as exc:
            topological_sort(nodes, edges)
        assert exc.value.cycle == ["a", "b", "c", "a"]
        assert "a → b → c → a" in str(exc.value)

    def test_self_loop(self):
        with pytest.raises(CycleError) as exc:
            topological_sort([node("a", "text", text="a")], [edge("a", "a")])
        assert exc.value.cycle == ["a", "a"]

    def test_unknown_edge_endpoint(self):
        with pytest.raises(GraphError, match="unknown node 'ghost'"):
            topological_sort([node("a", "text", text="a")], [edge("a", "ghost")])


class TestValidateGraph:
    def test_example_graph_is_valid(self):
        assert validate_workflow(example_graph()) == []

    def test_unknown_node_type(self):
        issues = validate_graph([node("x", "teleport")], [])
        assert codes(issues) == [IssueCode.UNKNOWN_NODE_TYPE]
        assert issues[0].node_id == "x"
        assert "unknown type 'teleport'" in issues[0].message

    def test_missing_required_config_field(self):
        issues = validate_graph([node("g", "gemini", system_prompt="hi")], [])
        assert codes(issues) == [IssueCode.MISSING_CONFIG]
        assert issues[0].field == "user_prompt"
        assert "missing required config field 'user_prompt'" in issues[0].message

    def test_several_missing_fields_on_one_node(self):
        issues = validate_graph([node("mail", "gmail", body="x")], [])
        assert sorted(i.field for i in issues) == ["subject", "to"]

    def test_invalid_static_value_and_unknown_field(self):
        issues = validate_graph([node("g", "gemini", user_prompt="hi", temperature="hot", user_promt="typo")], [])
        assert sorted(codes(issues)) == [IssueCode.INVALID_CONFIG, IssueCode.INVALID_CONFIG]
        assert {i.field for i in issues} == {"temperature", "user_promt"}

    def test_out_of_range_value(self):
        issues = validate_graph([node("d", "delay", seconds=60)], [])
        assert codes(issues) == [IssueCode.INVALID_CONFIG]
        assert issues[0].field == "seconds"

    def test_templated_value_is_not_type_checked_statically(self):
        nodes = [node("g", "gemini", user_prompt="hi", temperature="{{vars.temp}}")]
        assert validate_graph(nodes, [], [GraphVariable(key="temp", value="0.2")]) == []

    def test_broken_edge_references(self):
        nodes = [node("a", "text", text="a")]
        issues = validate_graph(nodes, [edge("a", "nowhere"), edge("ghost", "a")])
        assert codes(issues) == [IssueCode.BROKEN_EDGE, IssueCode.BROKEN_EDGE]
        assert "target 'nowhere'" in issues[0].message
        assert "source 'ghost'" in issues[1].message

    def test_cycle(self):
        nodes = [node(n, "text", text=n) for n in ("a", "b", "c")]
        issues = validate_graph(nodes, [edge("a", "b"), edge("b", "c"), edge("c", "a")])
        assert codes(issues) == [IssueCode.CYCLE]
        assert "a → b → c → a" in issues[0].message

    def test_duplicate_and_reserved_node_ids(self):
        issues = validate_graph([node("a", "text", text="1"), node("a", "text", text="2"), node("vars", "text", text="3")], [])
        assert codes(issues) == [IssueCode.DUPLICATE_NODE_ID, IssueCode.RESERVED_NODE_ID]

    def test_condition_edges_need_branch_handles(self):
        nodes = [
            node("check", "condition", left=1, operator="equals", right=1),
            node("yes", "text", text="y"),
            node("no", "text", text="n"),
        ]
        ok = validate_graph(nodes, [edge("check", "yes", "true"), edge("check", "no", "false")])
        assert ok == []
        bad = validate_graph(nodes, [edge("check", "yes"), edge("check", "no", "maybe")])
        assert codes(bad) == [IssueCode.INVALID_BRANCH, IssueCode.INVALID_BRANCH]

    def test_duplicate_input_and_output_names(self):
        nodes = [
            node("in1", "input", name="topic"),
            node("in2", "input", name="topic"),
            node("out1", "output", value="x"),
            node("out2", "output", value="y"),
        ]
        issues = validate_graph(nodes, [])
        assert codes(issues) == [IssueCode.DUPLICATE_NAME, IssueCode.DUPLICATE_NAME]


class TestReferenceValidation:
    def graph(self, prompt, variables=()):
        nodes = [
            node("input", "input", name="topic"),
            node("gemini", "gemini", user_prompt="About {{input.topic}}"),
            node("side", "text", text="unconnected"),
            node("mail", "gmail", to="a@example.com", subject="s", body=prompt),
        ]
        return validate_graph(nodes, chain("input", "gemini", "mail"), list(variables))

    def test_valid_references(self):
        prompt = "{{gemini.response}} {{input.value}} {{input.topic}} {{vars.sig}} {{system.execution_id}} {{gemini}}"
        assert self.graph(prompt, [GraphVariable(key="sig", value="-- me")]) == []

    @pytest.mark.parametrize(
        ("prompt", "expected"),
        [
            ("{{nobody.value}}", "'nobody' is not a node in this graph"),
            ("{{side.text}}", "node 'side' is not upstream of 'mail'"),
            ("{{mail.message_id}}", "cannot reference its own output"),
            ("{{gemini.respnse}}", "has no output 'respnse' (outputs: fallback_errors, mock, model, provider, provider_used, response)"),
            ("{{input.topik}}", "has no output 'topik'"),
            ("{{vars.missing}}", "no workflow variable 'missing'"),
            ("{{vars}}", "name a variable"),
            ("{{system.user}}", "unknown system value"),
            ("{{ }}", "empty reference"),
        ],
    )
    def test_unresolvable_references(self, prompt, expected):
        issues = self.graph(prompt)
        assert codes(issues) == [IssueCode.UNRESOLVABLE_REFERENCE]
        assert issues[0].node_id == "mail"
        assert issues[0].field == "body"
        assert expected in issues[0].message

    def test_reference_in_nested_config_reports_field_path(self):
        nodes = [node("out", "output", value={"items": ["ok", "{{ghost.value}}"]})]
        issues = validate_graph(nodes, [])
        assert issues[0].field == "value.items[1]"


class TestCredentialValidation:
    """With services, a provider lacking credentials is a validation error, never a silent mock."""

    @staticmethod
    def services(settings):
        from flowforge_engine import ExecutionServices

        return ExecutionServices(provider_settings=settings)

    def test_missing_keys_are_reported_per_provider(self):
        from flowforge_engine import ProviderSettings

        issues = validate_workflow(example_graph(), services=self.services(ProviderSettings()))
        assert codes(issues) == [IssueCode.AUTH_MISSING, IssueCode.AUTH_MISSING]
        gemini, gmail = issues
        assert (gemini.node_id, gemini.field) == ("gemini", "provider")
        assert "Authentication missing for provider 'gemini'" in gemini.message
        assert "GEMINI_API_KEY" in gemini.message
        assert gmail.node_id == "gmail" and "Authentication missing for provider 'gmail'" in gmail.message

    def test_configured_keys_validate(self):
        from flowforge_engine import ProviderSettings

        settings = (
            ProviderSettings()
            .with_account("gemini", api_key="k" * 20)
            .with_account("gmail", username="me@gmail.com", password="app-password")
        )
        assert validate_workflow(example_graph(), services=self.services(settings)) == []

    def test_fallback_providers_are_checked_too(self):
        from flowforge_engine import ProviderSettings

        graph = example_graph()
        graph.nodes[1].config["fallback"] = ["groq", "ollama"]
        settings = (
            ProviderSettings()
            .with_account("gemini", api_key="k" * 20)
            .with_account("gmail", username="me@gmail.com", password="app-password")
        )
        [issue] = validate_workflow(graph, services=self.services(settings))
        assert issue.code is IssueCode.AUTH_MISSING and issue.field == "fallback"
        assert "provider 'groq'" in issue.message  # ollama needs no key

    def test_explicit_mock_and_testing_mode_need_no_credentials(self):
        from flowforge_engine import ProviderSettings

        graph = example_graph()
        graph.nodes[1].config["provider"] = "mock"
        graph.nodes[2].config["auth"] = "mock"
        assert validate_workflow(graph, services=self.services(ProviderSettings())) == []
        assert validate_workflow(example_graph(), services=self.services(ProviderSettings(testing=True))) == []

    def test_without_services_credentials_are_not_checked(self):
        assert validate_workflow(example_graph()) == []
