import json
import logging

import httpx
import pytest
from pydantic import ValidationError

from flowforge_engine import get_node_definition
from flowforge_engine.errors import ProviderError
from flowforge_engine.models import GraphNode
from flowforge_engine.providers import LLM_PROVIDERS, MockEmailProvider, ProviderSettings
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


class FailingProvider:
    is_mock = False

    def __init__(self, name, message="rate limited (HTTP 429): quota exceeded"):
        self.name, self.message, self.calls = name, message, 0

    async def generate(self, **kwargs):
        self.calls += 1
        raise ProviderError(self.name, self.message)


class TestLLMNodes:
    LLM_TYPES = ["gemini", "groq", "openrouter", "ollama", "openai", "anthropic"]

    @pytest.mark.parametrize("node_type", LLM_TYPES)
    async def test_mock_response_in_testing_mode(self, node_type):
        prompt = "Summarize the following paragraph about the history of workflow engines"
        result = await run(node_type, {"user_prompt": prompt, "system_prompt": "Be brief."})
        assert result.success
        assert result.output["response"] == f"[MOCK RESPONSE to: {prompt[:50]}]"
        assert result.output["provider"] == result.output["provider_used"] == node_type
        assert result.output["mock"] is True
        assert result.output["fallback_errors"] == []
        # No model in config -> the provider's default model.
        assert result.output["model"] == LLM_PROVIDERS[node_type].default_model

    def test_config_has_the_required_fields(self):
        for node_type in self.LLM_TYPES:
            fields = get_node_definition(node_type).config_schema.model_fields
            expected = {"provider", "model", "system_prompt", "user_prompt", "temperature", "max_tokens", "fallback"}
            assert expected <= set(fields)
            assert fields["provider"].default == node_type

    def test_default_models(self):
        defaults = {name: info.default_model for name, info in LLM_PROVIDERS.items()}
        assert defaults == {
            "gemini": "gemini-3.5-flash-lite",
            "groq": "llama-3.3-70b-versatile",
            "openrouter": "openrouter/free",
            "ollama": "llama3.2",
            "openai": "gpt-4.1-mini",
            "anthropic": "claude-opus-5",
            "mock": "mock",
        }

    async def test_model_override_and_settings_default(self):
        settings = ProviderSettings(testing=True).with_account("groq", model="openai/gpt-oss-20b")
        services = ExecutionServices(provider_settings=settings)
        default = await run("groq", {"user_prompt": "hi"}, services=services)
        assert default.output["model"] == "openai/gpt-oss-20b"
        explicit = await run("groq", {"user_prompt": "hi", "model": "llama-3.1-8b-instant"}, services=services)
        assert explicit.output["model"] == "llama-3.1-8b-instant"

    async def test_explicit_mock_provider_works_without_testing_mode(self):
        services = ExecutionServices(provider_settings=ProviderSettings())  # no keys, not testing
        result = await run("gemini", {"user_prompt": "hi", "provider": "mock"}, services=services)
        assert result.success
        assert result.output["provider_used"] == "mock" and result.output["mock"] is True

    async def test_missing_key_fails_instead_of_mocking(self):
        services = ExecutionServices(provider_settings=ProviderSettings())  # no keys, not testing
        result = await run("gemini", {"user_prompt": "hi"}, services=services)
        assert not result.success
        assert result.error.startswith("Authentication missing for provider 'gemini'")
        assert "GEMINI_API_KEY" in result.error

    async def test_provider_error_becomes_node_failure(self):
        services = ExecutionServices(llm_providers={"gemini": FailingProvider("gemini")})
        result = await run("gemini", {"user_prompt": "hi"}, services=services)
        assert not result.success
        assert result.error == "gemini: rate limited (HTTP 429): quota exceeded"
        assert result.output["fallback_errors"][0]["provider"] == "gemini"

    async def test_fallback_chain_uses_the_next_provider(self, caplog):
        caplog.set_level(logging.INFO, logger="flowforge_engine")
        gemini, groq = FailingProvider("gemini"), FailingProvider("groq", "server error (HTTP 503): overloaded")
        services = ExecutionServices(
            provider_settings=ProviderSettings(testing=True),
            llm_providers={"gemini": gemini, "groq": groq},
        )
        result = await run(
            "gemini",
            {"user_prompt": "hi", "fallback": ["groq", "openrouter:google/gemma-4-31b-it:free", "ollama"]},
            services=services,
        )
        assert result.success
        assert result.output["provider"] == "gemini"
        assert result.output["provider_used"] == "openrouter"
        assert result.output["model"] == "google/gemma-4-31b-it:free"
        assert [e["provider"] for e in result.output["fallback_errors"]] == ["gemini", "groq"]
        assert gemini.calls == groq.calls == 1
        assert "LLM node answered" in caplog.text

    async def test_fallback_chain_all_failing(self):
        services = ExecutionServices(
            provider_settings=ProviderSettings(),
            llm_providers={"gemini": FailingProvider("gemini")},
        )
        result = await run("gemini", {"user_prompt": "hi", "fallback": ["groq"]}, services=services)
        assert not result.success
        assert result.error.startswith("All 2 providers failed: gemini: rate limited")
        assert "Authentication missing for provider 'groq'" in result.error

    def test_fallback_entries_must_name_known_providers(self):
        schema = get_node_definition("gemini").config_schema
        with pytest.raises(ValidationError, match="unknown provider 'mistral'"):
            schema.model_validate({"user_prompt": "hi", "fallback": ["mistral"]})
        with pytest.raises(ValidationError):
            schema.model_validate({"user_prompt": "hi", "provider": "mistral"})

    def test_required_providers(self):
        definition = get_node_definition("gemini")
        node = GraphNode(id="g", type="gemini", config={"user_prompt": "x", "fallback": ["groq", "ollama:qwen3"]})
        assert definition.required_providers(node) == [
            ("gemini", "provider"), ("groq", "fallback"), ("ollama", "fallback"),
        ]
        templated = GraphNode(id="g", type="gemini", config={"user_prompt": "x", "provider": "{{vars.p}}"})
        assert definition.required_providers(templated) == []

    def test_anthropic_temperature_capped_at_one(self):
        schema = get_node_definition("anthropic").config_schema
        with pytest.raises(ValidationError):
            schema.model_validate({"user_prompt": "hi", "temperature": 1.5})


class TestGmailNode:
    async def test_sends_through_mock_provider(self):
        result = await run(
            "gmail",
            {"to": "a@example.com, b@example.com", "cc": ["c@example.com"], "bcc": "d@example.com",
             "subject": "Hi", "body": "Body text", "html_body": "<p>Body</p>",
             "attachments": [{"filename": "notes.txt", "content": "hello"}]},
        )
        assert result.success
        assert result.output["status"] == "sent"
        assert result.output["message_id"].startswith("mock-")
        assert result.output["to"] == ["a@example.com", "b@example.com"]
        assert result.output["cc"] == ["c@example.com"]
        assert result.output["bcc"] == ["d@example.com"]
        assert result.output["attachments"] == ["notes.txt"]
        assert result.output["mock"] is True

        [sent] = MockEmailProvider.outbox()
        assert sent.message_id == result.output["message_id"]
        assert sent.body == "Body text" and sent.html_body == "<p>Body</p>"

    async def test_rejects_invalid_addresses(self):
        result = await run("gmail", {"to": "a@example.com", "bcc": "not-an-email", "subject": "s", "body": "b"})
        assert not result.success and "Invalid email address(es): not-an-email" in result.error
        assert MockEmailProvider.outbox() == []

    async def test_requires_a_recipient(self):
        result = await run("gmail", {"to": " , ", "subject": "s", "body": "b"})
        assert not result.success and "At least one recipient" in result.error

    async def test_missing_credentials_fail_the_node(self):
        services = ExecutionServices(provider_settings=ProviderSettings())
        result = await run("gmail", {"to": "a@example.com", "subject": "s", "body": "b"}, services=services)
        assert not result.success
        assert result.error.startswith("Authentication missing for provider 'gmail'")
        assert "SMTP_USER" in result.error

    async def test_mock_auth_sends_nothing_even_without_testing(self):
        services = ExecutionServices(provider_settings=ProviderSettings())
        config = {"auth": "mock", "to": "a@example.com", "subject": "s", "body": "b"}
        result = await run("gmail", config, services=services)
        assert result.success and result.output["mock"] is True


class TestGmailReadNode:
    async def test_reads_the_mock_mailbox(self):
        await run("gmail", {"to": "a@example.com", "subject": "Invoice 42", "body": "Please pay"})
        await run("gmail", {"to": "a@example.com", "subject": "Hello", "body": "Hi there"})
        result = await run("gmail_read", {"subject": "invoice"})
        assert result.success
        assert result.output["count"] == 1
        [message] = result.output["emails"]
        assert message["subject"] == "Invoice 42"
        assert message["body_text"] == "Please pay"
        assert result.output["folder"] == "INBOX" and result.output["mock"] is True

    def test_limits(self):
        schema = get_node_definition("gmail_read").config_schema
        with pytest.raises(ValidationError):
            schema.model_validate({"max_results": 500})
        assert schema.model_validate({}).unread_only is True

    async def test_missing_credentials_fail_the_node(self):
        services = ExecutionServices(provider_settings=ProviderSettings())
        result = await run("gmail_read", {}, services=services)
        assert not result.success and "Authentication missing for provider 'gmail'" in result.error


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
