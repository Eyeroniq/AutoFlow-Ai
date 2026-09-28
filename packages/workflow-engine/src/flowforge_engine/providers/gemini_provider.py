"""Google Gemini via the Google AI Studio API (google-genai SDK)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from flowforge_engine.errors import ProviderError
from flowforge_engine.providers.retry import RetryPolicy, classify_status, parse_retry_after, redact, with_retries
from flowforge_engine.providers.settings import LLM_PROVIDERS

if TYPE_CHECKING:
    from google.genai import Client

DEFAULT_MODEL = LLM_PROVIDERS["gemini"].default_model or ""
DEFAULT_EMBEDDING_MODEL = LLM_PROVIDERS["gemini"].embedding_model or ""


def _transport_errors() -> tuple[type[BaseException], ...]:
    # google-genai rides on httpx2; network failures surface as its TransportError.
    try:
        import httpx2
    except ImportError:  # pragma: no cover - depends on installed SDK version
        return ()
    return (httpx2.TransportError,)


def _retry_delay(details: Any) -> float | None:
    """The RetryInfo.retryDelay ("37s") Gemini attaches to 429 responses."""
    try:
        entries = details.get("error", {}).get("details", []) if isinstance(details, dict) else []
    except AttributeError:
        return None
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("@type", "")).endswith("RetryInfo"):
            match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(entry.get("retryDelay", "")))
            if match:
                return float(match.group(1))
    return None


def _quota_violations(details: Any) -> str:
    """ "GenerateRequestsPerDayPerProjectPerModel-FreeTier=20" from a 429's QuotaFailure, if any."""
    entries = details.get("error", {}).get("details", []) if isinstance(details, dict) else []
    violations = [
        f"{v.get('quotaId')}={v.get('quotaValue')}"
        for entry in entries
        if isinstance(entry, dict) and str(entry.get("@type", "")).endswith("QuotaFailure")
        for v in entry.get("violations", [])
        if isinstance(v, dict) and v.get("quotaId")
    ]
    return ", ".join(violations)


class GeminiProvider:
    name = "gemini"
    is_mock = False

    def __init__(
        self,
        api_key: str,
        *,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        retry: RetryPolicy | None = None,
        timeout: float = 60,
        client: Client | None = None,
    ):
        if client is None:
            try:
                from google import genai
                from google.genai import types
            except ImportError as exc:  # pragma: no cover - depends on installed extras
                raise ProviderError(
                    self.name, "the 'google-genai' package is not installed (flowforge-workflow-engine[gemini])"
                ) from exc
            # SDK retries stay off (its default): with_retries owns backoff.
            client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout * 1000)))
        self._client = client
        self._api_key = api_key
        self._embedding_model = embedding_model
        self._retry = retry or RetryPolicy()

    # --- error mapping ---------------------------------------------------------------

    def _error(self, exc: BaseException) -> ProviderError:
        from google.genai import errors

        if isinstance(exc, errors.APIError):
            headers = getattr(exc.response, "headers", None)
            retry_after = parse_retry_after(headers) or _retry_delay(exc.details)
            message = redact(exc.message or exc.status or "unknown error", self._api_key)
            quota = _quota_violations(exc.details) if exc.code == 429 else ""
            if quota:
                # Tells a daily free-tier cap apart from a per-minute one.
                message = f"{message} [quota exceeded: {quota}]"
            return classify_status(self.name, exc.code or 0, message, retry_after=retry_after)
        return ProviderError(
            self.name, f"connection error: {redact(str(exc) or type(exc).__name__, self._api_key)}", retryable=True
        )

    def _config(self, system_prompt: str, temperature: float, max_tokens: int) -> Any:
        from google.genai import types

        return types.GenerateContentConfig(
            system_instruction=system_prompt or None,
            temperature=temperature,
            max_output_tokens=max_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    # --- LLMProvider -----------------------------------------------------------------

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        from google.genai import errors

        config = self._config(system_prompt, temperature, max_tokens)

        async def call() -> Any:
            try:
                return await self._client.aio.models.generate_content(model=model, contents=user_prompt, config=config)
            except (errors.APIError, *_transport_errors()) as exc:
                raise self._error(exc) from exc

        response = await with_retries(call, self._retry)
        text = response.text
        if not text:
            raise ProviderError(self.name, f"no text in response ({self._empty_reason(response)})")
        return text

    @staticmethod
    def _empty_reason(response: Any) -> str:
        if response.candidates:
            reason = response.candidates[0].finish_reason
            hint = "; raise max_tokens (thinking counts toward it)" if str(reason).endswith("MAX_TOKENS") else ""
            return f"finish_reason={reason}{hint}"
        if response.prompt_feedback:
            return f"blocked: {response.prompt_feedback.block_reason}"
        return "empty response"

    async def stream(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        from google.genai import errors

        config = self._config(system_prompt, temperature, max_tokens)

        async def open_stream() -> Any:
            try:
                return await self._client.aio.models.generate_content_stream(
                    model=model, contents=user_prompt, config=config
                )
            except (errors.APIError, *_transport_errors()) as exc:
                raise self._error(exc) from exc

        chunks = await with_retries(open_stream, self._retry)
        produced = False
        last: Any = None
        try:
            async for chunk in chunks:
                last = chunk
                if chunk.text:
                    produced = True
                    yield chunk.text
        except (errors.APIError, *_transport_errors()) as exc:
            # Mid-stream failures aren't retried: text has already been handed out.
            raise self._error(exc) from exc
        if not produced:
            reason = self._empty_reason(last) if last is not None else "empty stream"
            raise ProviderError(self.name, f"no text in response ({reason})")

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        from google.genai import errors

        async def call() -> Any:
            try:
                return await self._client.aio.models.embed_content(
                    model=model or self._embedding_model, contents=text
                )
            except (errors.APIError, *_transport_errors()) as exc:
                raise self._error(exc) from exc

        response = await with_retries(call, self._retry)
        if not response.embeddings or response.embeddings[0].values is None:
            raise ProviderError(self.name, "embedding response contained no values")
        return list(response.embeddings[0].values)

    async def verify(self, model: str | None = None) -> dict[str, Any]:
        """Fetches the model's metadata: proves the key works and the model exists."""
        from google.genai import errors

        model = model or DEFAULT_MODEL

        async def call() -> Any:
            try:
                return await self._client.aio.models.get(model=model)
            except (errors.APIError, *_transport_errors()) as exc:
                raise self._error(exc) from exc

        info = await with_retries(call, self._retry)
        return {
            "model": (info.name or model).removeprefix("models/"),
            "display_name": info.display_name,
            "input_token_limit": info.input_token_limit,
            "output_token_limit": info.output_token_limit,
        }
