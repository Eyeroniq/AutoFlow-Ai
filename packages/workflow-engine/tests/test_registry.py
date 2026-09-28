import pytest

from flowforge_engine import (
    NodeConfig,
    NodeDefinition,
    NodeRegistry,
    NodeResult,
    WorkflowGraph,
    default_registry,
    execute_graph,
    get_node_definition,
    list_node_definitions,
    validate_workflow,
)
from flowforge_engine.testing import chain, make_context, node

BUILTIN = {"input", "output", "text", "condition", "delay", "gemini", "openai", "anthropic", "gmail", "http_request"}


def test_builtins_are_registered():
    assert BUILTIN <= set(default_registry.types())


def test_lookup_by_type():
    assert get_node_definition("gemini").label == "Gemini"
    assert get_node_definition("nope") is None


def test_listing_includes_json_config_schemas():
    catalog = {entry["type"]: entry for entry in list_node_definitions()}
    gemini = catalog["gemini"]
    assert gemini["category"] == "ai"
    assert gemini["icon"] == "sparkles"
    assert gemini["config_schema"]["required"] == ["user_prompt"]
    assert gemini["config_schema"]["properties"]["temperature"]["maximum"] == 2
    assert gemini["output_schema"]["required"] == ["response", "provider", "model", "mock"]
    assert catalog["condition"]["branches"] == ["true", "false"]
    for entry in catalog.values():
        assert {"type", "label", "description", "icon", "config_schema"} <= entry.keys()


def test_duplicate_registration_is_rejected():
    registry = NodeRegistry()

    class Echo(NodeDefinition[NodeConfig]):
        category, label, description, icon = "test", "Echo", "echo", "x"
        config_schema = NodeConfig

        async def execute(self, context, config):
            return NodeResult.ok()

    registry.add("echo", Echo)
    with pytest.raises(ValueError, match="already registered"):
        registry.add("echo", Echo)


def test_missing_metadata_is_rejected():
    class Incomplete(NodeDefinition[NodeConfig]):
        async def execute(self, context, config):
            return NodeResult.ok()

    with pytest.raises(TypeError, match="missing required attributes"):
        NodeRegistry().add("incomplete", Incomplete)


async def test_custom_node_type_runs_in_a_graph():
    registry = NodeRegistry()

    class ShoutConfig(NodeConfig):
        text: str

    @registry.register("shout")
    class ShoutNode(NodeDefinition[ShoutConfig]):
        category, label, description, icon = "test", "Shout", "uppercases text", "megaphone"
        config_schema = ShoutConfig

        async def execute(self, context, config):
            return NodeResult.ok(text=config.text.upper())

    graph = WorkflowGraph(nodes=[node("a", "shout", text="hi"), node("b", "shout", text="{{a.text}}!")], edges=chain("a", "b"))
    assert validate_workflow(graph, registry=registry) == []
    result = await execute_graph(graph, make_context(), registry=registry)
    assert result.final_output == {"b": {"text": "HI!"}}
    # The custom registry doesn't leak into the default one.
    assert "shout" not in default_registry
