from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from flowforge_engine.errors import ProviderError, ProviderNotSupportedError
from flowforge_engine.providers.retry import RetryPolicy, classify_status, parse_retry_after, redact, with_retries
from flowforge_engine.providers.settings import LLM_PROVIDERS

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

DEFAULT_MODEL = LLM_PROVIDERS["anthropic"].default_model or ""

# Current Claude models reject sampling parameters (temperature/top_p/top_k) with a 400
# (Sonnet 5 rejects non-default values). Only these older families still accept
# temperature; for everything else it is dropped.
_SAMPLING_MODEL_PREFIXES = (
    "claude-3",
    "claude-haiku-4",
    "claude-sonnet-4",
    "claude-opus-4-0",
    "claude-opus-4-1",
    "claude-opus-4-2",  # dated Opus 4.0 ids, e.g. claude-opus-4-20250514
    "claude-opus-4-5",
    "claude-opus-4-6",
)

# Models whose safety classifiers can decline a request. For these we opt into
# server-side fallbacks, which re-run a declined request on a recommended model.
_FALLBACK_MODELS = frozenset({"claude-opus-5", "claude-fable-5-1"})
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Above this, non-streaming requests risk the SDK's HTTP timeout guard, so generate()
# streams internally and collects the final message.
_NON_STREAMING_MAX_TOKENS = 16000


def accepts_temperature(model: str) -> bool:
    return model.startswith(_SAMPLING_MODEL_PREFIXES)


class AnthropicProvider:
    name = "anthropic"
    is_mock = False

    def __init__(
        self,
        api_key: str,
        *,
        retry: RetryPolicy | None = None,
        timeout: float = 60,
        client: AsyncAnthropic | None = None,
    ):
        if client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover - depends on installed extras
                raise ProviderError(
                    self.name, "the 'anthropic' package is not installed (flowforge-workflow-engine[anthropic])"
                ) from exc
            # max_retries=0: with_retries owns backoff (and honours retry-after).
            client = AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=0)
        self._client = client
        self._api_key = api_key
        self._retry = retry or RetryPolicy()

    def _error(self, exc: BaseException) -> ProviderError:
        import anthropic

        if isinstance(exc, anthropic.APIStatusError):
            message = redact(exc.message, self._api_key)
            return classify_status(
                self.name, exc.status_code, message, retry_after=parse_retry_after(exc.response.headers)
            )
        if isinstance(exc, anthropic.APITimeoutError):
            return ProviderError(self.name, "request timed out", retryable=True)
        return ProviderError(self.name, f"connection error: {redact(str(exc), self._api_key)}", retryable=True)

    def _params(self, system_prompt: str, user_prompt: str, model: str, temperature: float, max_tokens: int) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if system_prompt:
            params["system"] = system_prompt
        if accepts_temperature(model):
            # anthropic>=1 dropped `temperature` from the method signature; the older
            # models that still honour it get it via extra_body (merged into the JSON).
            params["extra_body"] = {"temperature": min(temperature, 1.0)}
        if model in _FALLBACK_MODELS:
            params["betas"] = [_FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

    def _messages(self, params: dict[str, Any]) -> Any:
        return self._client.beta.messages if "betas" in params else self._client.messages

    def _check(self, message: Any) -> str:
        # A classifier or model refusal is an HTTP 200 with stop_reason "refusal";
        # content is empty or partial, so it must be checked before reading text.
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ProviderError(self.name, f"request declined (refusal, category={category})")
        text = "".join(block.text for block in message.content if block.type == "text")
        if not text:
            raise ProviderError(self.name, f"no text in response (stop_reason={message.stop_reason})")
        return text

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        import anthropic

        params = self._params(system_prompt, user_prompt, model, temperature, max_tokens)

        async def call() -> Any:
            try:
                if max_tokens > _NON_STREAMING_MAX_TOKENS:
                    async with self._messages(params).stream(**params) as stream:
                        return await stream.get_final_message()
                return await self._messages(params).create(**params)
            except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
                raise self._error(exc) from exc

        return self._check(await with_retries(call, self._retry))

    async def stream(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        import anthropic

        params = self._params(system_prompt, user_prompt, model, temperature, max_tokens)

        async def open_stream() -> tuple[Any, Any]:
            # A stream manager can only be entered once, so each attempt builds a new one.
            manager = self._messages(params).stream(**params)
            try:
                return manager, await manager.__aenter__()
            except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
                raise self._error(exc) from exc

        manager, stream = await with_retries(open_stream, self._retry)
        try:
            async for text in stream.text_stream:
                yield text
            # Also raises on a mid-stream refusal (already-yielded text is partial).
            self._check(await stream.get_final_message())
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            raise self._error(exc) from exc
        finally:
            await manager.__aexit__(None, None, None)

    async def embed(
        self, text: str, model: str | None = None, dimensions: int | None = None, task: str | None = None
    ) -> list[float]:
        raise ProviderNotSupportedError(
            self.name, "Anthropic does not offer an embeddings API; use the gemini or ollama provider"
        )

    async def verify(self, model: str | None = None) -> dict[str, Any]:
        import anthropic

        model = model or DEFAULT_MODEL

        async def call() -> Any:
            try:
                return await self._client.models.retrieve(model)
            except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
                raise self._error(exc) from exc

        info = await with_retries(call, self._retry)
        return {"model": info.id, "display_name": info.display_name}
