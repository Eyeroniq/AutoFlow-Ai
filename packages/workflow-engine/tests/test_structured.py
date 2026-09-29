"""Structured Output: the JSON Schema checks, and the node's parse → validate → retry loop
against scripted LLM replies."""

import pytest

from flowforge_engine import ExecutionServices, NodeStatus, ProviderSettings, execute_node, get_node_definition
from flowforge_engine.jsonschema_lite import check_schema, validate
from flowforge_engine.testing import make_context, node

NOTES = {
    "type": "object",
    "required": ["summary", "decisions", "action_items"],
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["task", "owner"],
                "additionalProperties": False,
                "properties": {"task": {"type": "string"}, "owner": {"type": ["string", "null"]}, "due": {"type": "string"}},
            },
        },
        "sentiment": {"enum": ["positive", "neutral", "negative"]},
        "count": {"type": "integer", "minimum": 0},
    },
}


def test_valid_and_invalid_instances():
    good = {"summary": "Ship Friday.", "decisions": ["Ship"], "action_items": [{"task": "Tag", "owner": None}], "count": 2.0}
    assert validate(good, NOTES) == []
    bad = {"summary": "", "decisions": "Ship", "action_items": [{"task": 3, "extra": 1}], "sentiment": "angry", "count": -1}
    assert validate(bad, NOTES) == [
        "$.summary: shorter than 1 characters",
        "$.decisions: expected array, got str",
        "$.action_items[0].owner: is required",
        "$.action_items[0].task: expected string, got int",
        "$.action_items[0].extra: isn't allowed (not in the schema)",
        "$.sentiment: must be one of ['positive', 'neutral', 'negative']",
        "$.count: below the minimum 0",
    ]
    assert validate(True, {"type": "integer"}) == ["$: expected integer, got bool"]


def test_schema_checks():
    assert check_schema(NOTES) == []
    assert check_schema({"type": "text", "properties": {"a": {"type": "strng"}}, "oneOf": []}) == [
        "schema uses unsupported keywords: oneOf",
        "schema.type 'text' isn't one of string, number, integer, boolean, array, object, null",
        "schema.properties.a.type 'strng' isn't one of string, number, integer, boolean, array, object, null",
    ]
    with pytest.raises(ValueError, match="unsupported keywords: oneOf"):
        get_node_definition("structured_output").config_schema.model_validate({"prompt": "x", "schema": {"oneOf": []}})


class Scripted:
    is_mock = False

    def __init__(self, *replies):
        self.name, self.replies, self.prompts = "gemini", list(replies), []

    async def generate(self, system_prompt, user_prompt, model, temperature, max_tokens):
        self.prompts.append((system_prompt, user_prompt))
        return self.replies.pop(0)


async def run(llm, **config):
    services = ExecutionServices(provider_settings=ProviderSettings(testing=True), llm_providers={"gemini": llm})
    config = {"prompt": "Write notes from: {{t.text}}", "schema": NOTES, **config}
    return await execute_node(node("notes", "structured_output", **config),
                              make_context(services=services, node_outputs={"t": {"text": "We agreed to ship on Friday."}}))


async def test_a_valid_reply_on_the_first_try():
    llm = Scripted('```json\n{"summary": "Ship Friday.", "decisions": ["Ship on Friday"], "action_items": []}\n```')
    result = await run(llm)
    assert result.status is NodeStatus.SUCCESS, result.error
    assert result.output["data"] == {"summary": "Ship Friday.", "decisions": ["Ship on Friday"], "action_items": []}
    assert result.output["attempts"] == 1
    system, prompt = llm.prompts[0]
    assert prompt.startswith("Write notes from: We agreed to ship on Friday.") and '"required": ["summary"' in prompt
    assert system.endswith("You reply with JSON only.")


async def test_an_invalid_reply_is_retried_with_the_problems():
    llm = Scripted('{"summary": "Ship."}', '{"summary": "Ship.", "decisions": [], "action_items": [{"task": "Tag it", "owner": "Ana"}]}')
    result = await run(llm)
    assert result.status is NodeStatus.SUCCESS and result.output["attempts"] == 2
    assert "$.decisions: is required; $.action_items: is required" in llm.prompts[1][1]


async def test_it_fails_after_two_bad_replies():
    result = await run(Scripted("Sure! Here are the notes.", '{"summary": 5}'))
    assert result.status is NodeStatus.FAILED
    assert result.error.startswith("The model's reply didn't match the schema after 2 attempts: ")
    assert "$.decisions: is required" in result.error and "$.summary: expected string, got int" in result.error
    assert result.output["raw_reply"] == '{"summary": 5}'
