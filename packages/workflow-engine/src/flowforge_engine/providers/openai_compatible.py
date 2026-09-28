"""One adapter for every OpenAI-compatible chat API: Groq, OpenRouter, Ollama, OpenAI.

Presets pick the base URL, the max-tokens parameter name, embeddings support, and how
to check credentials; everything else is the standard /chat/completions contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from flowforge_engine.errors import ProviderError, ProviderNotSupportedError
from flowforge_engine.providers.retry import RetryPolicy, classify_status, parse_retry_after, redact, with_retries
from flowforge_engine.providers.settings import LLM_PROVIDERS

if TYPE_CHECKING:
    from openai import AsyncOpenAI

# OpenAI reasoning models only accept the default temperature.
_NO_TEMPERATURE_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def accepts_temperature(provider: str, model: str) -> bool:
    return not (provider == "openai" and model.startswith(_NO_TEMPERATURE_PREFIXES))


@dataclass(frozen=True)
class CompatPreset:
    name: str
    label: str
    base_url: str | None
    # Groq and OpenAI take max_completion_tokens; OpenRouter and Ollama document max_tokens.
    max_tokens_param: str = "max_completion_tokens"
    supports_embeddings: bool = False
    # "models": list models (proves the key where the endpoint requires auth);
    # "key": OpenRouter's /key endpoint (its model list is public).
    verify: str = "models"
    headers: dict[str, str] = field(default_factory=dict)
    requires_key: bool = True


PRESETS: dict[str, CompatPreset] = {
    "groq": CompatPreset("groq", "Groq", LLM_PROVIDERS["groq"].base_url),
    "openrouter": CompatPreset(
        "openrouter", "OpenRouter", LLM_PROVIDERS["openrouter"].base_url,
        max_tokens_param="max_tokens", verify="key",
        # Optional attribution headers (https://openrouter.ai/docs/api-reference/overview).
        headers={"X-Title": "FlowForge AI"},
    ),
    "ollama": CompatPreset(
        "ollama", "Ollama", LLM_PROVIDERS["ollama"].base_url,
        max_tokens_param="max_tokens", supports_embeddings=True, requires_key=False,
    ),
    "openai": CompatPreset("openai", "OpenAI", None, supports_embeddings=True),
}


class OpenAICompatibleProvider:
    is_mock = False

    def __init__(
        self,
        preset: CompatPreset | str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        embedding_model: str | None = None,
        retry: RetryPolicy | None = None,
        timeout: float = 60,
        client: AsyncOpenAI | None = None,
    ):
        self.preset = PRESETS[preset] if isinstance(preset, str) else preset
        self.name = self.preset.name
        self.base_url = base_url or self.preset.base_url
        self._api_key = api_key
        self._embedding_model = embedding_model
        self._retry = retry or RetryPolicy()
        if client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover - depends on installed extras
                raise ProviderError(
                    self.name, "the 'openai' package is not installed (flowforge-workflow-engine[openai])"
                ) from exc
            client = AsyncOpenAI(
                # Ollama ignores the key, but the SDK insists on one.
                api_key=api_key or "not-needed",
                base_url=self.base_url,
                default_headers=self.preset.headers or None,
                timeout=timeout,
                max_retries=0,  # with_retries owns backoff
            )
        self._client = client

    # --- error mapping ---------------------------------------------------------------

    def _error(self, exc: BaseException) -> ProviderError:
        import openai

        if isinstance(exc, openai.APIStatusError):
            message = redact(self._status_message(exc), self._api_key)
            return classify_status(
                self.name, exc.status_code, message, retry_after=parse_retry_after(exc.response.headers)
            )
        if isinstance(exc, openai.APITimeoutError):
            return ProviderError(self.name, "request timed out", retryable=True)
        where = f" at {self.base_url}" if self.base_url else ""
        hint = " — is `ollama serve` running?" if self.name == "ollama" else ""
        return ProviderError(
            self.name, f"cannot connect{where}{hint} ({redact(str(exc), self._api_key)})", retryable=True
        )

    @staticmethod
    def _status_message(exc: Any) -> str:
        # OpenRouter nests the upstream provider's reason under error.metadata.raw.
        body = exc.body if isinstance(getattr(exc, "body", None), dict) else {}
        raw = (body.get("metadata") or {}).get("raw") if isinstance(body.get("metadata"), dict) else None
        message = exc.message
        return f"{message} ({str(raw)[:300]})" if raw and str(raw) not in message else message

    async def _call(self, make: Any) -> Any:
        import openai

        async def call() -> Any:
            try:
                return await make()
            except (openai.APIStatusError, openai.APIConnectionError) as exc:
                raise self._error(exc) from exc

        return await with_retries(call, self._retry)

    # --- LLMProvider -----------------------------------------------------------------

    def _chat_params(
        self, system_prompt: str, user_prompt: str, model: str, temperature: float, max_tokens: int
    ) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})
        params: dict[str, Any] = {"model": model, "messages": messages}
        if self.preset.max_tokens_param == "max_tokens":
            params["max_tokens"] = max_tokens
        else:
            params["max_completion_tokens"] = max_tokens
        if accepts_temperature(self.name, model):
            params["temperature"] = temperature
        return params

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        params = self._chat_params(system_prompt, user_prompt, model, temperature, max_tokens)
        response = await self._call(lambda: self._client.chat.completions.create(**params))
        if not response.choices:
            raise ProviderError(self.name, "response contained no choices")
        choice = response.choices[0]
        refusal = getattr(choice.message, "refusal", None)
        if refusal:
            raise ProviderError(self.name, f"request refused: {refusal}")
        if not choice.message.content:
            hint = "; raise max_tokens" if choice.finish_reason == "length" else ""
            raise ProviderError(self.name, f"no text in response (finish_reason={choice.finish_reason}{hint})")
        return choice.message.content

    async def stream(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        import openai

        params = self._chat_params(system_prompt, user_prompt, model, temperature, max_tokens)
        chunks = await self._call(lambda: self._client.chat.completions.create(**params, stream=True))
        produced = False
        finish_reason = None
        try:
            async for chunk in chunks:
                if not chunk.choices:  # e.g. a trailing usage-only chunk
                    continue
                choice = chunk.choices[0]
                finish_reason = choice.finish_reason or finish_reason
                if choice.delta and choice.delta.content:
                    produced = True
                    yield choice.delta.content
        except (openai.APIStatusError, openai.APIConnectionError) as exc:
            raise self._error(exc) from exc
        if not produced:
            raise ProviderError(self.name, f"no text in response (finish_reason={finish_reason})")

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        model = model or self._embedding_model
        if not self.preset.supports_embeddings or not model:
            raise ProviderNotSupportedError(
                self.name, f"{self.preset.label} has no embeddings API here; use the gemini or ollama provider"
            )
        response = await self._call(lambda: self._client.embeddings.create(model=model, input=text))
        return list(response.data[0].embedding)

    async def verify(self, model: str | None = None) -> dict[str, Any]:
        if self.preset.verify == "key":
            info = await self._call(lambda: self._client.get("/key", cast_to=object))
            data = info.get("data", {}) if isinstance(info, dict) else {}
            return {
                "model": model,
                "label": data.get("label"),
                "is_free_tier": data.get("is_free_tier"),
                "limit_remaining": data.get("limit_remaining"),
            }

        page = await self._call(lambda: self._client.models.list())
        ids = [m.id for m in page.data]
        details: dict[str, Any] = {"models_available": len(ids), "model": model}
        if model:
            # Ollama lists pulled models as "llama3.2:latest".
            available = model in ids or f"{model}:latest" in ids
            details["model_available"] = available
            if not available and self.name == "ollama":
                raise ProviderError(
                    self.name, f"model '{model}' is not pulled on {self.base_url}; run `ollama pull {model}`"
                )
        return details
