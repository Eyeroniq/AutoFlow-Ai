"""Provider factory, retry policy, and real adapters exercised against mocked HTTP (no network, no keys)."""

import json

import pytest

from flowforge_engine.errors import MissingCredentialsError, ProviderError, ProviderNotSupportedError
from flowforge_engine.providers import (
    MockEmailProvider,
    MockLLMProvider,
    ProviderSettings,
    RetryPolicy,
    get_email_provider,
    get_llm_provider,
    get_mailbox_provider,
    with_retries,
)
from flowforge_engine.providers.anthropic_provider import AnthropicProvider
from flowforge_engine.providers.gemini_provider import GeminiProvider
from flowforge_engine.providers.imap_provider import IMAPEmailProvider
from flowforge_engine.providers.openai_compatible import OpenAICompatibleProvider
from flowforge_engine.providers.retry import backoff_delay, classify_status, parse_retry_after
from flowforge_engine.providers.smtp_provider import SMTPEmailProvider

httpx2 = pytest.importorskip("httpx2")

FAST = RetryPolicy(max_retries=2, base_delay=0, max_delay=1)


class TestFactoryPolicy:
    @pytest.mark.parametrize("name", ["gemini", "groq", "openrouter", "openai", "anthropic"])
    def test_missing_key_raises_instead_of_mocking(self, name):
        with pytest.raises(MissingCredentialsError, match=f"Authentication missing for provider '{name}'"):
            get_llm_provider(name, ProviderSettings())

    def test_missing_key_message_names_the_env_var(self):
        with pytest.raises(MissingCredentialsError, match="Set GROQ_API_KEY on the server"):
            get_llm_provider("groq", ProviderSettings())

    @pytest.mark.parametrize(
        ("name", "cls"),
        [("gemini", GeminiProvider), ("groq", OpenAICompatibleProvider), ("openrouter", OpenAICompatibleProvider),
         ("openai", OpenAICompatibleProvider), ("anthropic", AnthropicProvider)],
    )
    def test_real_adapter_when_key_set(self, name, cls):
        settings = ProviderSettings().with_account(name, api_key="test-key-123456")
        provider = get_llm_provider(name, settings)
        assert isinstance(provider, cls) and provider.name == name and not provider.is_mock

    def test_presets_point_at_the_right_base_urls(self):
        settings = (
            ProviderSettings()
            .with_account("groq", api_key="k" * 10)
            .with_account("openrouter", api_key="k" * 10)
        )
        assert get_llm_provider("groq", settings).base_url == "https://api.groq.com/openai/v1"
        assert get_llm_provider("openrouter", settings).base_url == "https://openrouter.ai/api/v1"

    def test_ollama_needs_no_key_and_honours_base_url(self):
        default = get_llm_provider("ollama", ProviderSettings())
        assert isinstance(default, OpenAICompatibleProvider)
        assert default.base_url == "http://localhost:11434/v1"
        custom = ProviderSettings().with_account("ollama", base_url="http://host.docker.internal:11434/v1")
        assert get_llm_provider("ollama", custom).base_url == "http://host.docker.internal:11434/v1"

    def test_testing_mode_hands_out_mocks_even_with_keys(self):
        settings = ProviderSettings(testing=True).with_account("gemini", api_key="real-looking-key")
        assert isinstance(get_llm_provider("gemini", settings), MockLLMProvider)
        assert isinstance(get_email_provider("gmail", settings), MockEmailProvider)
        assert isinstance(get_mailbox_provider("gmail", settings), MockEmailProvider)

    def test_explicit_mock_provider(self):
        assert isinstance(get_llm_provider("mock", ProviderSettings()), MockLLMProvider)
        assert isinstance(get_email_provider("mock", ProviderSettings()), MockEmailProvider)

    def test_reads_environment_by_default(self, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        with pytest.raises(MissingCredentialsError):
            get_llm_provider("gemini")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        assert isinstance(get_llm_provider("gemini"), GeminiProvider)

    def test_blank_key_counts_as_missing(self):
        settings = ProviderSettings().with_account("openai", api_key="  ")
        assert not settings.has_credentials("openai")

    def test_email_needs_user_and_password(self):
        with pytest.raises(MissingCredentialsError, match="SMTP_USER and SMTP_PASSWORD"):
            get_email_provider("gmail", ProviderSettings())
        settings = ProviderSettings().with_account("gmail", username="me@gmail.com", password="abcd efgh ijkl mnop")
        assert isinstance(get_email_provider("gmail", settings), SMTPEmailProvider)
        assert isinstance(get_mailbox_provider("gmail", settings), IMAPEmailProvider)

    def test_unknown_providers(self):
        with pytest.raises(ValueError, match="Unknown LLM provider 'mistral'"):
            get_llm_provider("mistral", ProviderSettings())
        with pytest.raises(ValueError):
            get_email_provider("outlook", ProviderSettings())

    def test_secret_keys_are_not_printed(self):
        settings = ProviderSettings().with_account("gemini", api_key="super-secret-value")
        assert "super-secret-value" not in repr(settings)
        assert "super-secret-value" not in settings.model_dump_json()


class TestSettingsFromEnv:
    def test_maps_every_variable(self):
        settings = ProviderSettings.from_mapping({
            "TESTING": "true",
            "GEMINI_API_KEY": "g-key", "GEMINI_MODEL": "gemini-3.5-flash-lite", "GEMINI_EMBEDDING_MODEL": "gemini-embedding-001",
            "GROQ_API_KEY": "q-key", "GROQ_MODEL": "qwen/qwen3.8-27b",
            "OPENROUTER_API_KEY": "o-key", "OPENROUTER_MODEL": "google/gemma-4-31b-it:free",
            "OLLAMA_BASE_URL": "http://host.docker.internal:11434/v1", "OLLAMA_MODEL": "qwen3",
            "SMTP_USER": "me@gmail.com", "SMTP_PASSWORD": "app-pass", "SMTP_PORT": "465",
            "LLM_MAX_RETRIES": "5", "LLM_RETRY_MAX_DELAY_SECONDS": "10", "LLM_REQUEST_TIMEOUT_SECONDS": "15",
        })
        assert settings.testing is True
        assert settings.default_model("gemini") == "gemini-3.5-flash-lite"
        assert settings.embedding_model("gemini") == "gemini-embedding-001"
        assert settings.default_model("groq") == "qwen/qwen3.8-27b"
        assert settings.default_model("openrouter") == "google/gemma-4-31b-it:free"
        assert settings.base_url("ollama") == "http://host.docker.internal:11434/v1"
        assert settings.default_model("ollama") == "qwen3"
        assert settings.gmail.username == "me@gmail.com" and settings.gmail.resolved_smtp_security == "ssl"
        assert settings.retry.max_retries == 5 and settings.retry.max_delay == 10
        assert settings.request_timeout_seconds == 15

    def test_blank_values_use_defaults(self):
        settings = ProviderSettings.from_mapping({"GEMINI_API_KEY": "", "GEMINI_MODEL": " ", "SMTP_HOST": "", "SMTP_PORT": ""})
        assert not settings.has_credentials("gemini")
        assert settings.default_model("gemini") == "gemini-3.5-flash-lite"
        assert settings.gmail.smtp_host == "smtp.gmail.com" and settings.gmail.smtp_port == 587
        assert settings.gmail.resolved_smtp_security == "starttls"

    def test_google_api_key_alias(self):
        assert ProviderSettings.from_mapping({"GOOGLE_API_KEY": "k"}).has_credentials("gemini")


class TestRetry:
    async def test_retries_transient_errors_then_succeeds(self):
        calls, delays = [], []

        async def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise ProviderError("groq", "rate limited (HTTP 429): slow down", status_code=429, retryable=True)
            return "ok"

        async def sleep(seconds):
            delays.append(seconds)

        policy = RetryPolicy(max_retries=3, base_delay=1, max_delay=30)
        assert await with_retries(flaky, policy, sleep=sleep) == "ok"
        assert len(calls) == 3
        # Exponential with equal jitter: retry n waits between base*2^(n-1)/2 and base*2^(n-1).
        assert 0.5 <= delays[0] <= 1 and 1 <= delays[1] <= 2

    async def test_gives_up_with_attempt_count(self):
        async def always_503():
            raise ProviderError("gemini", "server error (HTTP 503): overloaded", status_code=503, retryable=True)

        async def sleep(_):
            pass

        with pytest.raises(ProviderError, match=r"overloaded \(gave up after 3 attempts\)") as info:
            await with_retries(always_503, RetryPolicy(max_retries=2), sleep=sleep)
        assert info.value.attempts == 3

    async def test_non_retryable_errors_fail_fast(self):
        calls = []

        async def bad_request():
            calls.append(1)
            raise ProviderError("groq", "API error 400: bad model", status_code=400)

        with pytest.raises(ProviderError, match="API error 400"):
            await with_retries(bad_request, RetryPolicy(max_retries=5))
        assert len(calls) == 1

    async def test_honours_retry_after_but_not_beyond_max_delay(self):
        delays = []

        async def sleep(seconds):
            delays.append(seconds)

        attempts = []

        async def limited():
            attempts.append(1)
            if len(attempts) == 1:
                raise ProviderError("openrouter", "rate limited", retryable=True, retry_after=2)
            raise ProviderError("openrouter", "rate limited", retryable=True, retry_after=90)

        with pytest.raises(ProviderError, match="asked to wait 90s, longer than the 30s retry limit"):
            await with_retries(limited, RetryPolicy(max_retries=5, max_delay=30), sleep=sleep)
        assert len(delays) == 1 and 2 <= delays[0] <= 2.25

    def test_backoff_is_capped(self):
        policy = RetryPolicy(base_delay=1, max_delay=5)
        assert all(backoff_delay(n, policy) <= 5 for n in range(1, 20))

    def test_classify_status(self):
        assert classify_status("x", 429, "m").retryable
        assert classify_status("x", 503, "m").retryable
        assert classify_status("x", 408, "m").retryable
        assert not classify_status("x", 400, "m").retryable
        assert "check the API key" in str(classify_status("x", 401, "m"))

    def test_parse_retry_after(self):
        assert parse_retry_after({"retry-after": "7"}) == 7
        assert parse_retry_after({"retry-after-ms": "1500"}) == 1.5
        assert parse_retry_after({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None
        assert parse_retry_after({}) is None


# --- adapters against mocked HTTP --------------------------------------------------------


def recorder(*responses):
    """A mock transport that captures requests and replays `responses` (the last one repeats)."""
    captured = []

    def handler(request):
        captured.append(request)
        response = responses[min(len(captured), len(responses)) - 1]
        return response() if callable(response) else response

    return httpx2.MockTransport(handler), captured


def json_response(body, status=200, headers=None):
    return lambda: httpx2.Response(status, json=body, headers=headers or {})


def sse_response(lines):
    body = "".join(f"{line}\n\n" for line in lines).encode()
    return lambda: httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})


class TestOpenAICompatibleAdapter:
    @staticmethod
    def provider(name, transport, **kwargs):
        from openai import AsyncOpenAI, DefaultAsyncHttpxClient

        from flowforge_engine.providers.openai_compatible import PRESETS

        base_url = kwargs.pop("base_url", None) or PRESETS[name].base_url
        client = AsyncOpenAI(
            api_key="test-key-123456", base_url=base_url, max_retries=0,
            default_headers=PRESETS[name].headers or None,
            http_client=DefaultAsyncHttpxClient(transport=transport),
        )
        return OpenAICompatibleProvider(name, api_key="test-key-123456", base_url=base_url, client=client, retry=FAST, **kwargs)

    @staticmethod
    def completion(content, refusal=None, finish_reason="stop"):
        return {
            "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{
                "index": 0, "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }],
        }

    async def test_groq_generate(self):
        transport, captured = recorder(json_response(self.completion("Hi from Groq")))
        text = await self.provider("groq", transport).generate("sys", "Hello", "openai/gpt-oss-20b", 0.2, 50)
        assert text == "Hi from Groq"
        [request] = captured
        assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key-123456"
        body = json.loads(request.content)
        assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "Hello"}]
        assert body["max_completion_tokens"] == 50 and body["temperature"] == 0.2

    async def test_openrouter_uses_max_tokens_and_attribution_header(self):
        transport, captured = recorder(json_response(self.completion("free!")))
        text = await self.provider("openrouter", transport).generate("", "Hi", "google/gemma-4-31b-it:free", 0.5, 64)
        assert text == "free!"
        body = json.loads(captured[0].content)
        assert str(captured[0].url) == "https://openrouter.ai/api/v1/chat/completions"
        assert body["model"] == "google/gemma-4-31b-it:free"
        assert body["max_tokens"] == 64 and "max_completion_tokens" not in body
        assert captured[0].headers["x-title"] == "FlowForge AI"

    async def test_openai_reasoning_models_skip_temperature(self):
        transport, captured = recorder(json_response(self.completion("ok")))
        await self.provider("openai", transport, base_url="https://api.openai.com/v1").generate("", "Hello", "gpt-5-mini", 0.2, 50)
        assert "temperature" not in json.loads(captured[0].content)

    async def test_429_is_retried_with_retry_after(self):
        transport, captured = recorder(
            json_response({"error": {"message": "Rate limit reached", "type": "tokens"}}, 429, {"retry-after": "0"}),
            json_response(self.completion("after retry")),
        )
        assert await self.provider("groq", transport).generate("", "Hi", "m", 0.2, 10) == "after retry"
        assert len(captured) == 2

    async def test_persistent_5xx_surfaces_a_clear_error(self):
        transport, captured = recorder(json_response({"error": {"message": "upstream overloaded"}}, 503))
        with pytest.raises(ProviderError, match=r"groq: server error \(HTTP 503\).*gave up after 3 attempts"):
            await self.provider("groq", transport).generate("", "Hi", "m", 0.2, 10)
        assert len(captured) == 3

    async def test_auth_error_is_not_retried(self):
        transport, captured = recorder(json_response({"error": {"message": "Invalid API Key"}}, 401))
        with pytest.raises(ProviderError, match="authentication failed .* check the API key"):
            await self.provider("groq", transport).generate("", "Hi", "m", 0.2, 10)
        assert len(captured) == 1

    async def test_refusal_and_empty_content(self):
        transport, _ = recorder(json_response(self.completion(None, refusal="I can't help with that")))
        with pytest.raises(ProviderError, match="refused"):
            await self.provider("openrouter", transport).generate("", "Hello", "m", 0.2, 50)
        transport, _ = recorder(json_response(self.completion(None, finish_reason="length")))
        with pytest.raises(ProviderError, match="finish_reason=length; raise max_tokens"):
            await self.provider("openrouter", transport).generate("", "Hello", "m", 0.2, 50)

    async def test_stream(self):
        def chunk(content, finish=None):
            return "data: " + json.dumps({
                "id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
                "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}],
            })

        transport, captured = recorder(sse_response([chunk("Hel"), chunk("lo"), chunk(None, "stop"), "data: [DONE]"]))
        pieces = [p async for p in self.provider("groq", transport).stream("", "Hi", "m", 0.2, 10)]
        assert pieces == ["Hel", "lo"]
        assert json.loads(captured[0].content)["stream"] is True

    async def test_ollama_embeddings(self):
        transport, captured = recorder(json_response({
            "object": "list", "model": "nomic-embed-text",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }))
        provider = self.provider("ollama", transport, embedding_model="nomic-embed-text")
        assert await provider.embed("hi") == [0.1, 0.2]
        assert str(captured[0].url) == "http://localhost:11434/v1/embeddings"

    async def test_groq_and_openrouter_have_no_embeddings(self):
        transport, captured = recorder(json_response({}))
        for name in ("groq", "openrouter"):
            with pytest.raises(ProviderNotSupportedError, match="no embeddings API"):
                await self.provider(name, transport).embed("hi")
        assert captured == []

    async def test_ollama_connection_error_hint(self):
        def refuse(request):
            raise httpx2.ConnectError("connection refused", request=request)

        provider = self.provider("ollama", httpx2.MockTransport(refuse))
        with pytest.raises(ProviderError, match="is `ollama serve` running"):
            await provider.generate("", "Hi", "llama3.2", 0.2, 10)

    async def test_verify_lists_models_and_checks_ollama_pull(self):
        models = json_response({"object": "list", "data": [
            {"id": "llama3.2:latest", "object": "model", "created": 0, "owned_by": "library"},
        ]})
        transport, _ = recorder(models)
        assert (await self.provider("ollama", transport).verify("llama3.2"))["model_available"] is True
        transport, _ = recorder(models)
        with pytest.raises(ProviderError, match="run `ollama pull qwen3`"):
            await self.provider("ollama", transport).verify("qwen3")

    async def test_verify_fails_when_the_model_isnt_available_to_the_key(self):
        transport, _ = recorder(json_response({"object": "list", "data": [
            {"id": "openai/gpt-oss-20b", "object": "model", "created": 0, "owned_by": "OpenAI"},
        ]}))
        with pytest.raises(ProviderError, match="model 'llama-3.3-70b-versatile' isn't available to it; set GROQ_MODEL"):
            await self.provider("groq", transport).verify("llama-3.3-70b-versatile")
        transport, _ = recorder(json_response({"object": "list", "data": [
            {"id": "openai/gpt-oss-20b", "object": "model", "created": 0, "owned_by": "OpenAI"},
        ]}))
        assert (await self.provider("groq", transport).verify("openai/gpt-oss-20b"))["model_available"] is True

    async def test_openrouter_verify_uses_key_endpoint(self):
        transport, captured = recorder(json_response({"data": {"label": "sk-or-v1-abc...", "is_free_tier": True, "limit_remaining": None}}))
        details = await self.provider("openrouter", transport).verify("openrouter/free")
        assert str(captured[0].url) == "https://openrouter.ai/api/v1/key"
        assert details["is_free_tier"] is True

    async def test_api_key_is_redacted_from_errors(self):
        transport, _ = recorder(json_response({"error": {"message": "key test-key-123456 is invalid"}}, 400))
        with pytest.raises(ProviderError) as info:
            await self.provider("groq", transport).generate("", "Hi", "m", 0.2, 10)
        assert "test-key-123456" not in str(info.value) and "[REDACTED]" in str(info.value)


class TestGeminiAdapter:
    @staticmethod
    def provider(transport):
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key="test-key-123456",
            http_options=types.HttpOptions(httpx_async_client=httpx2.AsyncClient(transport=transport)),
        )
        return GeminiProvider("test-key-123456", client=client, retry=FAST)

    @staticmethod
    def candidate(text, finish="STOP"):
        return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}]}

    async def test_generate(self):
        transport, captured = recorder(json_response(self.candidate("Hi from Gemini")))
        text = await self.provider(transport).generate("Be brief.", "Hello", "gemini-3.8-flash", 0.4, 256)
        assert text == "Hi from Gemini"
        [request] = captured
        assert "gemini-3.8-flash:generateContent" in request.url.path
        body = json.loads(request.content)
        assert body["contents"][0]["parts"][0]["text"] == "Hello"
        assert body["systemInstruction"]["parts"][0]["text"] == "Be brief."
        assert body["generationConfig"] == {"temperature": 0.4, "maxOutputTokens": 256}

    async def test_empty_response_raises(self):
        transport, _ = recorder(json_response({"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "MAX_TOKENS"}]}))
        with pytest.raises(ProviderError, match="no text in response .*raise max_tokens"):
            await self.provider(transport).generate("", "Hello", "gemini-3.8-flash", 0.4, 8)

    async def test_429_uses_retry_info_then_succeeds(self):
        quota = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded", "details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "0s"},
        ]}}
        transport, captured = recorder(json_response(quota, 429), json_response(self.candidate("recovered")))
        assert await self.provider(transport).generate("", "Hi", "gemini-3.8-flash", 0.4, 64) == "recovered"
        assert len(captured) == 2

    async def test_daily_quota_names_the_quota_and_does_not_wait(self):
        quota = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota", "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
                {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"},
            ]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "48s"},
        ]}}
        transport, captured = recorder(json_response(quota, 429))
        with pytest.raises(ProviderError) as info:
            await self.provider(transport).generate("", "Hi", "gemini-3.8-flash", 0.4, 64)
        message = str(info.value)
        assert "rate limited (HTTP 429)" in message
        assert "quota exceeded: GenerateRequestsPerDayPerProjectPerModel-FreeTier=20" in message
        assert "asked to wait 48s, longer than the 1s retry limit" in message
        assert len(captured) == 1

    async def test_retired_model_error_is_clear(self):
        gone = {"error": {"code": 404, "status": "NOT_FOUND", "message": "This model models/gemini-2.5-flash is no longer available"}}
        transport, captured = recorder(json_response(gone, 404))
        with pytest.raises(ProviderError, match="gemini: API error 404: This model models/gemini-2.5-flash is no longer available"):
            await self.provider(transport).generate("", "Hi", "gemini-2.5-flash", 0.4, 64)
        assert len(captured) == 1

    async def test_stream(self):
        transport, captured = recorder(sse_response([
            "data: " + json.dumps(self.candidate("Hel", finish=None)),
            "data: " + json.dumps(self.candidate("lo")),
        ]))
        pieces = [p async for p in self.provider(transport).stream("", "Hi", "gemini-3.8-flash", 0.4, 64)]
        assert pieces == ["Hel", "lo"]
        assert "streamGenerateContent" in captured[0].url.path

    async def test_stream_retries_an_overload_before_the_first_token(self):
        # The SDK sends the request lazily, so the 503 surfaces on the first chunk.
        busy = {"error": {"code": 503, "status": "UNAVAILABLE", "message": "This model is currently experiencing high demand."}}
        transport, captured = recorder(
            json_response(busy, 503),
            sse_response(["data: " + json.dumps(self.candidate("Back"))]),
        )
        pieces = [p async for p in self.provider(transport).stream("", "Hi", "gemini-3.8-flash", 0.4, 64)]
        assert pieces == ["Back"] and len(captured) == 2

    async def test_stream_gives_up_on_a_persistent_overload(self):
        busy = {"error": {"code": 503, "status": "UNAVAILABLE", "message": "high demand"}}
        transport, captured = recorder(json_response(busy, 503))
        with pytest.raises(ProviderError, match="server error"):
            _ = [p async for p in self.provider(transport).stream("", "Hi", "gemini-3.8-flash", 0.4, 64)]
        assert len(captured) >= 2  # retried, then gave up

    async def test_embed(self):
        transport, captured = recorder(json_response({"embeddings": [{"values": [0.5, 0.25]}]}))
        assert await self.provider(transport).embed("hello") == [0.5, 0.25]
        assert "gemini-embedding-2" in captured[0].url.path

    async def test_verify_gets_the_model(self):
        transport, captured = recorder(json_response({
            "name": "models/gemini-3.8-flash", "displayName": "Gemini 3.8 Flash",
            "inputTokenLimit": 1048576, "outputTokenLimit": 65536,
        }))
        details = await self.provider(transport).verify("gemini-3.8-flash")
        assert details["model"] == "gemini-3.8-flash" and details["display_name"] == "Gemini 3.8 Flash"
        assert captured[0].method == "GET"


class TestAnthropicAdapter:
    @staticmethod
    def provider(transport):
        from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

        client = AsyncAnthropic(
            api_key="test-key-123456", max_retries=0, http_client=DefaultAsyncHttpxClient(transport=transport)
        )
        return AnthropicProvider("test-key-123456", client=client, retry=FAST)

    @staticmethod
    def message(content, stop_reason="end_turn", model="claude-opus-5", stop_details=None):
        return {
            "id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "stop_details": stop_details,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    async def test_opus_5_uses_fallbacks_and_drops_temperature(self):
        transport, captured = recorder(json_response(self.message([
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world"},
        ])))
        text = await self.provider(transport).generate("Be brief.", "Hi", "claude-opus-5", 0.3, 1000)

        assert text == "Hello world"
        [request] = captured
        body = json.loads(request.content)
        assert request.url.path == "/v1/messages"
        assert "server-side-fallback-2026-07-01" in request.headers["anthropic-beta"]
        assert body["fallbacks"] == "default"
        assert body["system"] == "Be brief."
        assert body["max_tokens"] == 1000
        assert "temperature" not in body

    async def test_older_model_gets_temperature_and_no_fallbacks(self):
        transport, captured = recorder(json_response(self.message([{"type": "text", "text": "ok"}], model="claude-haiku-4-5")))
        await self.provider(transport).generate("", "Hi", "claude-haiku-4-5", 0.3, 100)
        body = json.loads(captured[0].content)
        assert body["temperature"] == 0.3
        assert "fallbacks" not in body and "system" not in body
        assert "anthropic-beta" not in captured[0].headers

    async def test_refusal_raises(self):
        transport, _ = recorder(json_response(self.message(
            [], stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": None}
        )))
        with pytest.raises(ProviderError, match=r"declined \(refusal, category=cyber\)"):
            await self.provider(transport).generate("", "Hi", "claude-opus-5", 1.0, 100)

    async def test_api_error_is_wrapped(self):
        transport, _ = recorder(json_response(
            {"type": "error", "error": {"type": "invalid_request_error", "message": "bad model"}}, 400
        ))
        with pytest.raises(ProviderError, match="anthropic: API error 400"):
            await self.provider(transport).generate("", "Hi", "claude-opus-5", 1.0, 100)

    async def test_overloaded_529_is_retried(self):
        overloaded = json_response({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, 529)
        transport, captured = recorder(overloaded, json_response(self.message([{"type": "text", "text": "ok"}])))
        assert await self.provider(transport).generate("", "Hi", "claude-opus-5", 1.0, 100) == "ok"
        assert len(captured) == 2

    async def test_stream(self):
        def event(name, data):
            return f"event: {name}\ndata: {json.dumps(data)}"

        transport, captured = recorder(sse_response([
            event("message_start", {"type": "message_start", "message": self.message([], stop_reason=None, model="claude-haiku-4-5")}),
            event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            event("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}}),
            event("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}}),
            event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}}),
            event("message_stop", {"type": "message_stop"}),
        ]))
        pieces = [p async for p in self.provider(transport).stream("", "Hi", "claude-haiku-4-5", 0.3, 100)]
        assert pieces == ["Hel", "lo"]
        assert json.loads(captured[0].content)["stream"] is True

    async def test_no_embeddings(self):
        with pytest.raises(NotImplementedError, match="does not offer an embeddings API"):
            await AnthropicProvider("k").embed("text")


class TestMockProviders:
    async def test_mock_llm_is_deterministic(self):
        mock = MockLLMProvider("gemini")
        prompt = "x" * 80
        first = await mock.generate("", prompt, "m", 0.5, 100)
        assert first == f"[MOCK RESPONSE to: {'x' * 50}]"
        assert first == await mock.generate("other system", prompt, "m2", 1.0, 5)
        assert "".join([p async for p in mock.stream("", prompt, "m", 0.5, 100)]) == first

    async def test_mock_embeddings(self):
        mock = MockLLMProvider()
        vector = await mock.embed("hello")
        assert len(vector) == MockLLMProvider.EMBEDDING_DIMENSIONS
        assert vector == await mock.embed("hello")
        assert vector != await mock.embed("goodbye")
        assert abs(sum(v * v for v in vector) - 1) < 1e-4
