import pytest

from flowforge_engine import VariableResolutionError, find_references, resolve_string, resolve_value
from flowforge_engine.testing import make_context
from flowforge_engine.variables import build_scope, parse_reference

SCOPE = {
    "gemini": {"response": "Hello there", "usage": {"tokens": 42}},
    "http": {"body": {"items": [{"name": "first"}, {"name": "second"}], "count": 2, "ok": True}},
    "input": {"value": "cats", "topic": "cats"},
    "empty": {"value": None},
    "vars": {"recipient": "a@example.com"},
    "system": {"workflow_id": "wf-1", "execution_id": "ex-1", "node_id": "n1"},
}


class TestParseReference:
    def test_dot_path(self):
        assert parse_reference("gemini.response") == ["gemini", "response"]

    def test_bracket_and_dot_indexes(self):
        assert parse_reference("http.body.items[1].name") == ["http", "body", "items", 1, "name"]
        assert parse_reference("http.body.items.1.name") == ["http", "body", "items", "1", "name"]

    def test_surrounding_whitespace_is_ignored(self):
        assert parse_reference("  gemini.response ") == ["gemini", "response"]

    @pytest.mark.parametrize("bad", ["", "   ", "a..b", "a.b c", "[0]", "a.b]", "a{b}"])
    def test_invalid_syntax(self, bad):
        with pytest.raises(VariableResolutionError):
            parse_reference(bad)


class TestResolveString:
    def test_whole_string_reference_keeps_type(self):
        assert resolve_string("{{http.body.count}}", SCOPE) == 2
        assert resolve_string("{{http.body.ok}}", SCOPE) is True
        assert resolve_string("{{http.body.items}}", SCOPE) == SCOPE["http"]["body"]["items"]
        assert resolve_string("  {{ gemini }}  ", SCOPE) == SCOPE["gemini"]

    def test_interpolation_stringifies(self):
        assert resolve_string("Say: {{gemini.response}}!", SCOPE) == "Say: Hello there!"
        assert resolve_string("n={{http.body.count}} ok={{http.body.ok}}", SCOPE) == "n=2 ok=true"
        assert resolve_string("[{{empty.value}}]", SCOPE) == "[]"
        assert resolve_string("{{gemini.usage}} used", SCOPE) == '{"tokens": 42} used'

    def test_nested_paths_and_indexes(self):
        assert resolve_string("{{http.body.items[0].name}}", SCOPE) == "first"
        assert resolve_string("{{http.body.items.1.name}}", SCOPE) == "second"
        assert resolve_string("{{gemini.usage.tokens}}", SCOPE) == 42

    def test_user_input_workflow_variable_and_system(self):
        assert resolve_string("{{input.topic}}", SCOPE) == "cats"
        assert resolve_string("{{vars.recipient}}", SCOPE) == "a@example.com"
        assert resolve_string("{{system.execution_id}}", SCOPE) == "ex-1"

    def test_no_references_passes_through(self):
        assert resolve_string("plain text with { braces }", SCOPE) == "plain text with { braces }"

    def test_multiple_references(self):
        assert resolve_string("{{input.value}} and {{input.topic}}", SCOPE) == "cats and cats"


class TestResolutionErrors:
    def test_unknown_root(self):
        with pytest.raises(VariableResolutionError, match="'missing' has no output"):
            resolve_string("{{missing.value}}", SCOPE)

    def test_missing_key_lists_available_keys(self):
        with pytest.raises(VariableResolutionError) as exc:
            resolve_string("{{gemini.respnse}}", SCOPE)
        assert exc.value.expression == "gemini.respnse"
        assert "key 'respnse' not found in gemini (available: response, usage)" in str(exc.value)

    def test_index_out_of_range(self):
        with pytest.raises(VariableResolutionError, match="index 5 out of range"):
            resolve_string("{{http.body.items[5]}}", SCOPE)

    def test_non_numeric_index_into_list(self):
        with pytest.raises(VariableResolutionError, match="is a list"):
            resolve_string("{{http.body.items.first}}", SCOPE)

    def test_reading_into_a_scalar(self):
        with pytest.raises(VariableResolutionError, match="cannot read 'length'"):
            resolve_string("{{gemini.response.length}}", SCOPE)

    def test_error_message_format(self):
        with pytest.raises(VariableResolutionError) as exc:
            resolve_string("{{vars.nope}}", SCOPE)
        assert str(exc.value).startswith("Cannot resolve '{{vars.nope}}':")


class TestResolveValue:
    def test_recurses_into_dicts_and_lists(self):
        config = {
            "to": ["{{vars.recipient}}", "static@example.com"],
            "payload": {"summary": "{{gemini.response}}", "count": "{{http.body.count}}"},
            "retries": 3,
        }
        assert resolve_value(config, SCOPE) == {
            "to": ["a@example.com", "static@example.com"],
            "payload": {"summary": "Hello there", "count": 2},
            "retries": 3,
        }

    def test_find_references(self):
        config = {"a": "{{x.y}} and {{ z }}", "b": ["{{vars.k}}"], "c": 1}
        assert find_references(config) == ["x.y", "z", "vars.k"]


def test_build_scope_from_context():
    context = make_context(
        variables={"recipient": "r@example.com"},
        node_outputs={"gemini": {"response": "hi"}},
        node_id="gmail",
    )
    scope = build_scope(context)
    assert resolve_string("{{gemini.response}} {{vars.recipient}} {{system.node_id}}", scope) == (
        "hi r@example.com gmail"
    )
    assert scope["system"]["execution_id"] == context.execution_id
