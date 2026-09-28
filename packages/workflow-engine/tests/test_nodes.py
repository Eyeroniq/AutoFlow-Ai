import json

import httpx
import pytest
from pydantic import ValidationError

from flowforge_engine import get_node_definition
from flowforge_engine.errors import ProviderError
from flowforge_engine.providers import MockEmailProvider
from flowforge_engine.services import ExecutionServices
from flowforge_engine.testing import make_context, mock_services


async def run(node_type, config, **context_kwargs):
    definition = get_node_definition(node_type)
    typed = definition.config_schema.model_validate(config)
    return await definition.execute(make_context(**context_kwargs), typed)


class TestInputNode:
    async def test_text_from_run_inputs(self):
        result = await run("input", {"name": "topic"}, inputs={"topic": "cats"})
        assert result.success
        assert result.output == {"value": "cats", "topic": "cats"}

    async def test_name_defaults_to_node_id(self):
        result = await run("input", {}, inputs={"question": "why?"}, node_id="question")
        assert result.output == {"value": "why?", "question": "why?"}

    async def test_name_value_does_not_collide(self):
        result = await run("input", {"name": "value"}, inputs={"value": "x"})
        assert result.output == {"value": "x"}

    async def test_default_used_when_not_supplied(self):
        result = await run("input", {"name": "n", "input_type": "number", "default": 5})
        assert result.output["value"] == 5

    @pytest.mark.parametrize(("raw", "expected"), [(3, 3), (2.5, 2.5), ("42", 42), (" 3.5 ", 3.5)])
    async def test_number_coercion(self, raw, expected):
        result = await run("input", {"name": "n", "input_type": "number"}, inputs={"n": raw})
        assert result.success and result.output["value"] == expected

    @pytest.mark.parametrize("raw", ["abc", True, [1], "nan"])
    async def test_number_rejects_non_numbers(self, raw):
        result = await run("input", {"name": "n", "input_type": "number"}, inputs={"n": raw})
        assert not result.success
        assert result.error.startswith("Input 'n': expected")

    async def test_text_rejects_non_text(self):
        result = await run("input", {"name": "t"}, inputs={"t": 12})
        assert not result.success and "expected text, got int" in result.error

    async def test_json_parses_strings_and_accepts_objects(self):
        parsed = await run("input", {"name": "j", "input_type": "json"}, inputs={"j": '{"a": [1, 2]}'})
        assert parsed.output["value"] == {"a": [1, 2]}
        passthrough = await run("input", {"name": "j", "input_type": "json"}, inputs={"j": {"b": True}})
        assert passthrough.output["value"] == {"b": True}

    async def test_json_rejects_invalid_json(self):
        result = await run("input", {"name": "j", "input_type": "json"}, inputs={"j": "{nope"})
        assert not result.success and "expected valid JSON" in result.error

    async def test_missing_required_input(self):
        result = await run("input", {"name": "topic"})
        assert not result.success and result.error == "Missing required input 'topic'"

    async def test_missing_optional_input(self):
        result = await run("input", {"name": "topic", "required": False})
        assert result.success and result.output == {"value": None, "topic": None}


async def test_output_node():
    result = await run("output", {"name": "summary", "value": {"a": 1}})
    assert result.output == {"name": "summary", "value": {"a": 1}}


async def test_text_node():
    result = await run("text", {"text": "hello"})
    assert result.output == {"text": "hello"}


class TestConditionNode:
    @pytest.mark.parametrize(
        ("left", "operator", "right", "expected"),
        [
            ("a", "equals", "a", True),
            ("10", "equals", 10, True),
            ("a", "equals", "b", False),
            ("a", "not_equals", "b", True),
            ("hello world", "contains", "world", True),
            (["x", "y"], "contains", "y", True),
            ({"key": 1}, "contains", "key", True),
            ("hello", "contains", "bye", False),
            (5, "greater_than", "3", True),
            (2, "greater_than", 3, False),
            ("1.5", "less_than", 2, True),
        ],
    )
    async def test_operators(self, left, operator, right, expected):
        result = await run("condition", {"left": left, "operator": operator, "right": right})
        assert result.success
        assert result.output == {"result": expected, "branch": "true" if expected else "false"}

    async def test_greater_than_needs_numbers(self):
        result = await run("condition", {"left": "abc", "operator": "greater_than", "right": 1})
        assert not result.success and "needs two numbers" in result.error

    async def test_contains_needs_a_container(self):
        result = await run("condition", {"left": 5, "operator": "contains", "right": 1})
        assert not result.success

    def test_unknown_operator_is_a_config_error(self):
        with pytest.raises(ValidationError):
            get_node_definition("condition").config_schema.model_validate(
                {"left": 1, "operator": "matches", "right": 1}
            )


class TestLLMNodes:
    @pytest.mark.parametrize("node_type", ["gemini", "openai", "anthropic"])
    async def test_mock_response(self, node_type):
        prompt = "Summarize the following paragraph about the history of workflow engines"
        result = await run(node_type, {"user_prompt": prompt, "system_prompt": "Be brief."})
        assert result.success
        assert result.output["response"] == f"[MOCK RESPONSE to: {prompt[:50]}]"
        assert result.output["provider"] == node_type
        assert result.output["mock"] is True
        assert result.output["model"] == get_node_definition(node_type).config_schema.model_fields["model"].default

    async def test_default_models(self):
        defaults = {t: get_node_definition(t).config_schema.model_fields["model"].default for t in ("gemini", "openai", "anthropic")}
        assert defaults == {"gemini": "gemini-2.5-flash", "openai": "gpt-4.1-mini", "anthropic": "claude-opus-5"}

    async def test_provider_error_becomes_node_failure(self):
        class FailingProvider:
            name, is_mock = "gemini", False

            async def generate(self, **kwargs):
                raise ProviderError("gemini", "API error 429: quota exceeded")

        services = ExecutionServices(llm_providers={"gemini": FailingProvider()})
        definition = get_node_definition("gemini")
        result = await definition.execute(
            make_context(services=services), definition.config_schema.model_validate({"user_prompt": "hi"})
        )
        assert not result.success
        assert result.error == "gemini: API error 429: quota exceeded"

    def test_anthropic_temperature_capped_at_one(self):
        schema = get_node_definition("anthropic").config_schema
        with pytest.raises(ValidationError):
            schema.model_validate({"user_prompt": "hi", "temperature": 1.5})


class TestGmailNode:
    async def test_sends_through_mock_provider(self):
        result = await run(
            "gmail",
            {"to": "a@example.com, b@example.com", "cc": ["c@example.com"], "subject": "Hi", "body": "Body text"},
        )
        assert result.success
        assert result.output["status"] == "sent"
        assert result.output["message_id"].startswith("mock-")
        assert result.output["to"] == ["a@example.com", "b@example.com"]
        assert result.output["cc"] == ["c@example.com"]
        assert result.output["mock"] is True

        [sent] = MockEmailProvider.outbox()
        assert sent.message_id == result.output["message_id"]
        assert sent.body == "Body text"

    async def test_rejects_invalid_addresses(self):
        result = await run("gmail", {"to": "not-an-email", "subject": "s", "body": "b"})
        assert not result.success and "Invalid email address(es): not-an-email" in result.error
        assert MockEmailProvider.outbox() == []

    async def test_requires_a_recipient(self):
        result = await run("gmail", {"to": " , ", "subject": "s", "body": "b"})
        assert not result.success and "At least one recipient" in result.error


class TestHTTPRequestNode:
    @staticmethod
    def services(handler):
        return mock_services(http_transport=httpx.MockTransport(handler))

    async def test_get_json(self):
        def handler(request):
            assert request.url.params["q"] == "flowforge"
            assert request.headers["x-api-key"] == "k"
            return httpx.Response(200, json={"hits": [1, 2]})

        result = await run(
            "http_request",
            {"url": "https://api.example.com/search", "query": {"q": "flowforge"}, "headers": {"X-Api-Key": "k"}},
            services=self.services(handler),
        )
        assert result.success
        assert result.output["status_code"] == 200
        assert result.output["body"] == {"hits": [1, 2]}
        assert result.output["ok"] is True

    async def test_post_json_body(self):
        def handler(request):
            return httpx.Response(201, json={"received": json.loads(request.content)})

        result = await run(
            "http_request",
            {"method": "POST", "url": "https://api.example.com/items", "body": {"name": "x"}},
            services=self.services(handler),
        )
        assert result.output["body"] == {"received": {"name": "x"}}

    async def test_text_body(self):
        result = await run(
            "http_request", {"url": "https://example.com"},
            services=self.services(lambda r: httpx.Response(200, text="<html>hi</html>", headers={"content-type": "text/html"})),
        )
        assert result.output["body"] == "<html>hi</html>" and result.output["truncated"] is False

    async def test_error_status_fails_by_default(self):
        result = await run(
            "http_request", {"url": "https://example.com/missing"},
            services=self.services(lambda r: httpx.Response(404, text="nope")),
        )
        assert not result.success
        assert result.error == "HTTP 404 from GET https://example.com/missing"
        assert result.output["status_code"] == 404

    async def test_error_status_allowed_when_configured(self):
        result = await run(
            "http_request", {"url": "https://example.com/missing", "fail_on_error": False},
            services=self.services(lambda r: httpx.Response(404, text="nope")),
        )
        assert result.success and result.output["ok"] is False

    async def test_connection_error(self):
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        result = await run("http_request", {"url": "https://down.example.com"}, services=self.services(handler))
        assert not result.success and "ConnectError" in result.error

    async def test_rejects_non_http_urls(self):
        result = await run("http_request", {"url": "file:///etc/passwd"})
        assert not result.success and "must start with http" in result.error


class TestDelayNode:
    async def test_waits(self):
        result = await run("delay", {"seconds": 0.01})
        assert result.output == {"waited_seconds": 0.01}

    def test_capped_at_ten_seconds(self):
        schema = get_node_definition("delay").config_schema
        with pytest.raises(ValidationError):
            schema.model_validate({"seconds": 11})
        with pytest.raises(ValidationError):
            schema.model_validate({"seconds": -1})
