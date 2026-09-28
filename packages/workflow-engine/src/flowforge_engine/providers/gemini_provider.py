from __future__ import annotations

from typing import TYPE_CHECKING

from flowforge_engine.errors import ProviderError

if TYPE_CHECKING:
    from google.genai import Client

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"


class GeminiProvider:
    name = "gemini"
    is_mock = False

    def __init__(self, api_key: str, *, client: Client | None = None):
        if client is None:
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - depends on installed extras
                raise ProviderError(
                    self.name, "the 'google-genai' package is not installed (flowforge-workflow-engine[gemini])"
                ) from exc
            client = genai.Client(api_key=api_key)
        self._client = client

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=system_prompt or None,
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=model, contents=user_prompt, config=config
            )
        except errors.APIError as exc:
            raise ProviderError(self.name, f"API error {exc.code}: {exc.message}") from exc

        text = response.text
        if not text:
            reason = None
            if response.candidates:
                reason = response.candidates[0].finish_reason
            elif response.prompt_feedback:
                reason = response.prompt_feedback.block_reason
            raise ProviderError(self.name, f"no text in response (reason={reason})")
        return text

    async def embed(self, text: str) -> list[float]:
        from google.genai import errors

        try:
            response = await self._client.aio.models.embed_content(
                model=DEFAULT_EMBEDDING_MODEL, contents=text
            )
        except errors.APIError as exc:
            raise ProviderError(self.name, f"embedding failed: {exc.message}") from exc
        if not response.embeddings or response.embeddings[0].values is None:
            raise ProviderError(self.name, "embedding response contained no values")
        return list(response.embeddings[0].values)
