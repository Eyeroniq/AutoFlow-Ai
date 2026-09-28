from __future__ import annotations

from typing import TYPE_CHECKING, Any

from flowforge_engine.errors import ProviderError

if TYPE_CHECKING:
    from openai import AsyncOpenAI

DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"

# Reasoning models only accept the default temperature.
_NO_TEMPERATURE_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def accepts_temperature(model: str) -> bool:
    return not model.startswith(_NO_TEMPERATURE_PREFIXES)


class OpenAIProvider:
    name = "openai"
    is_mock = False

    def __init__(self, api_key: str, *, client: AsyncOpenAI | None = None):
        if client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover - depends on installed extras
                raise ProviderError(
                    self.name, "the 'openai' package is not installed (flowforge-workflow-engine[openai])"
                ) from exc
            client = AsyncOpenAI(api_key=api_key)
        self._client = client

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        import openai

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        params: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
        }
        if accepts_temperature(model):
            params["temperature"] = temperature

        try:
            response = await self._client.chat.completions.create(**params)
        except openai.APIStatusError as exc:
            raise ProviderError(self.name, f"API error {exc.status_code}: {exc.message}") from exc
        except openai.APIConnectionError as exc:
            raise ProviderError(self.name, f"connection error: {exc}") from exc

        choice = response.choices[0]
        if choice.message.refusal:
            raise ProviderError(self.name, f"request refused: {choice.message.refusal}")
        if not choice.message.content:
            raise ProviderError(self.name, f"no text in response (finish_reason={choice.finish_reason})")
        return choice.message.content

    async def embed(self, text: str) -> list[float]:
        import openai

        try:
            response = await self._client.embeddings.create(model=DEFAULT_EMBEDDING_MODEL, input=text)
        except openai.APIError as exc:
            raise ProviderError(self.name, f"embedding failed: {exc}") from exc
        return list(response.data[0].embedding)
