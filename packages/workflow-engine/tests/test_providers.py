"""Provider factory + real adapters exercised against mocked HTTP (no network, no keys)."""

import json

import pytest

from flowforge_engine.errors import ProviderError
from flowforge_engine.providers import (
    MockEmailProvider,
    MockLLMProvider,
    ProviderSettings,
    get_email_provider,
    get_llm_provider,
)
from flowforge_engine.providers.anthropic_provider import AnthropicProvider
from flowforge_engine.providers.gemini_provider import GeminiProvider
from flowforge_engine.providers.gmail_provider import GmailProvider
from flowforge_engine.providers.openai_provider import OpenAIProvider

httpx2 = pytest.importorskip("httpx2")


class TestFactory:
    @pytest.mark.parametrize("name", ["gemini", "openai", "anthropic"])
    def test_falls_back_to_mock_without_key(self, name, caplog):
        provider = get_llm_provider(name, ProviderSettings())
        assert isinstance(provider, MockLLMProvider)
        assert provider.name == name
        assert "using MockLLMProvider (mock mode)" in caplog.text

    def test_reads_environment_by_default(self, monkeypatch):
        assert isinstance(get_llm_provider("gemini"), MockLLMProvider)
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        assert isinstance(get_llm_provider("gemini"), GeminiProvider)

    def test_blank_key_counts_as_missing(self):
        assert isinstance(get_llm_provider("openai", ProviderSettings(openai_api_key="  ")), MockLLMProvider)

    @pytest.mark.parametrize(
        ("name", "cls"), [("gemini", GeminiProvider), ("openai", OpenAIProvider), ("anthropic", AnthropicProvider)]
    )
    def test_real_adapter_when_key_set(self, name, cls):
        settings = ProviderSettings(**{f"{name}_api_key": "test-key"})
        assert isinstance(get_llm_provider(name, settings), cls)

    def test_case_insensitive_and_unknown(self):
        assert isinstance(get_llm_provider("Gemini", ProviderSettings()), MockLLMProvider)
        with pytest.raises(ValueError, match="Unknown LLM provider 'mistral'"):
            get_llm_provider("mistral")

    def test_email_is_always_mock_this_phase(self):
        assert isinstance(get_email_provider("gmail"), MockEmailProvider)
        with pytest.raises(ValueError):
            get_email_provider("outlook")

    def test_secret_keys_are_not_printed(self):
        assert "super-secret" not in repr(ProviderSettings(gemini_api_key="super-secret"))


class TestMocks:
    async def test_mock_llm_is_deterministic(self):
        mock = MockLLMProvider("gemini")
        prompt = "x" * 80
        first = await mock.generate("", prompt, "m", 0.5, 100)
        assert first == f"[MOCK RESPONSE to: {'x' * 50}]"
        assert first == await mock.generate("other system", prompt, "m2", 1.0, 5)

    async def test_mock_embeddings(self):
        mock = MockLLMProvider()
        vector = await mock.embed("hello")
        assert len(vector) == MockLLMProvider.EMBEDDING_DIMENSIONS
        assert vector == await mock.embed("hello")
        assert vector != await mock.embed("goodbye")
        assert abs(sum(v * v for v in vector) - 1) < 1e-4

    async def test_gmail_provider_is_a_stub(self):
        with pytest.raises(NotImplementedError):
            await GmailProvider().send_email(["a@example.com"], [], "s", "b")


def recorder(response_json, status=200):
    """A mock transport that captures requests and replies with fixed JSON."""
    captured = []

    def handler(request):
        captured.append(request)
        return httpx2.Response(status, json=response_json)

    return httpx2.MockTransport(handler), captured


class TestAnthropicAdapter:
    @staticmethod
    def provider(transport):
        from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

        client = AsyncAnthropic(
            api_key="test-key", max_retries=0, http_client=DefaultAsyncHttpxClient(transport=transport)
        )
        return AnthropicProvider("test-key", client=client)

    @staticmethod
    def message(content, stop_reason="end_turn", model="claude-opus-5", stop_details=None):
        return {
            "id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "stop_details": stop_details,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    async def test_opus_5_uses_fallbacks_and_drops_temperature(self):
        transport, captured = recorder(self.message([
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world"},
        ]))
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
        transport, captured = recorder(self.message([{"type": "text", "text": "ok"}], model="claude-haiku-4-5"))
        await self.provider(transport).generate("", "Hi", "claude-haiku-4-5", 0.3, 100)
        body = json.loads(captured[0].content)
        assert body["temperature"] == 0.3
        assert "fallbacks" not in body and "system" not in body
        assert "anthropic-beta" not in captured[0].headers

    async def test_refusal_raises(self):
        transport, _ = recorder(self.message(
            [], stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": None}
        ))
        with pytest.raises(ProviderError, match=r"declined \(refusal, category=cyber\)"):
            await self.provider(transport).generate("", "Hi", "claude-opus-5", 1.0, 100)

    async def test_api_error_is_wrapped(self):
        transport, _ = recorder(
            {"type": "error", "error": {"type": "invalid_request_error", "message": "bad model"}}, status=400
        )
        with pytest.raises(ProviderError, match="anthropic: API error 400"):
            await self.provider(transport).generate("", "Hi", "claude-opus-5", 1.0, 100)

    async def test_no_embeddings(self):
        with pytest.raises(NotImplementedError, match="does not offer an embeddings API"):
            await AnthropicProvider("k").embed("text")


class TestOpenAIAdapter:
    @staticmethod
    def provider(transport):
        from openai import AsyncOpenAI, DefaultAsyncHttpxClient

        client = AsyncOpenAI(api_key="test-key", max_retries=0, http_client=DefaultAsyncHttpxClient(transport=transport))
        return OpenAIProvider("test-key", client=client)

    @staticmethod
    def completion(content, refusal=None):
        return {
            "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "gpt-4.1-mini",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }],
        }

    async def test_generate(self):
        transport, captured = recorder(self.completion("Hi from GPT"))
        text = await self.provider(transport).generate("sys", "Hello", "gpt-4.1-mini", 0.2, 50)
        assert text == "Hi from GPT"
        body = json.loads(captured[0].content)
        assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "Hello"}]
        assert body["max_completion_tokens"] == 50
        assert body["temperature"] == 0.2

    async def test_reasoning_models_skip_temperature(self):
        transport, captured = recorder(self.completion("ok"))
        await self.provider(transport).generate("", "Hello", "gpt-5-mini", 0.2, 50)
        assert "temperature" not in json.loads(captured[0].content)

    async def test_refusal_raises(self):
        transport, _ = recorder(self.completion(None, refusal="I can't help with that"))
        with pytest.raises(ProviderError, match="refused"):
            await self.provider(transport).generate("", "Hello", "gpt-4.1-mini", 0.2, 50)

    async def test_embed(self):
        transport, _ = recorder({
            "object": "list", "model": "text-embedding-3-small",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        })
        assert await self.provider(transport).embed("hi") == [0.1, 0.2]


class TestGeminiAdapter:
    @staticmethod
    def provider(transport):
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key="test-key",
            http_options=types.HttpOptions(httpx_async_client=httpx2.AsyncClient(transport=transport)),
        )
        return GeminiProvider("test-key", client=client)

    async def test_generate(self):
        transport, captured = recorder({
            "candidates": [{"content": {"role": "model", "parts": [{"text": "Hi from Gemini"}]}, "finishReason": "STOP"}],
        })
        text = await self.provider(transport).generate("Be brief.", "Hello", "gemini-2.5-flash", 0.4, 256)
        assert text == "Hi from Gemini"
        [request] = captured
        assert "gemini-2.5-flash:generateContent" in request.url.path
        body = json.loads(request.content)
        assert body["contents"][0]["parts"][0]["text"] == "Hello"
        assert body["systemInstruction"]["parts"][0]["text"] == "Be brief."
        assert body["generationConfig"] == {"temperature": 0.4, "maxOutputTokens": 256}

    async def test_empty_response_raises(self):
        transport, _ = recorder({"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "SAFETY"}]})
        with pytest.raises(ProviderError, match="no text in response"):
            await self.provider(transport).generate("", "Hello", "gemini-2.5-flash", 0.4, 256)
