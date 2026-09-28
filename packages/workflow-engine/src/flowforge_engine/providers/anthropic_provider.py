from __future__ import annotations

from typing import TYPE_CHECKING, Any

from flowforge_engine.errors import ProviderError

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

DEFAULT_MODEL = "claude-opus-5"

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


def accepts_temperature(model: str) -> bool:
    return model.startswith(_SAMPLING_MODEL_PREFIXES)


class AnthropicProvider:
    name = "anthropic"
    is_mock = False

    def __init__(self, api_key: str, *, client: AsyncAnthropic | None = None):
        if client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover - depends on installed extras
                raise ProviderError(
                    self.name, "the 'anthropic' package is not installed (flowforge-workflow-engine[anthropic])"
                ) from exc
            client = AsyncAnthropic(api_key=api_key)
        self._client = client

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        import anthropic

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

        try:
            if model in _FALLBACK_MODELS:
                response = await self._client.beta.messages.create(
                    betas=[_FALLBACK_BETA], fallbacks="default", **params
                )
            else:
                response = await self._client.messages.create(**params)
        except anthropic.APIStatusError as exc:
            raise ProviderError(self.name, f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(self.name, f"connection error: {exc}") from exc

        # A classifier or model refusal is an HTTP 200 with stop_reason "refusal";
        # content is empty or partial, so it must be checked before reading text.
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise ProviderError(self.name, f"request declined (refusal, category={category})")

        text = "".join(block.text for block in response.content if block.type == "text")
        if not text:
            raise ProviderError(self.name, f"no text in response (stop_reason={response.stop_reason})")
        return text

    async def embed(self, text: str) -> list[float]:
        raise NotImplementedError(
            "Anthropic does not offer an embeddings API; use the Gemini or OpenAI provider for embeddings"
        )
