from __future__ import annotations

import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from flowforge_engine.models import NodeContext, NodeResult
from flowforge_engine.netguard import BlockedDestination, check_url, guarded_transport
from flowforge_engine.registry import NodeConfig, NodeDefinition, register_node

# Response bodies are stored in execution history; cap what we keep.
MAX_BODY_CHARS = 100_000


class HTTPRequestConfig(NodeConfig):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "GET"
    url: str = Field(min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    body: Any = Field(default=None, description="Objects/lists are sent as JSON; anything else as text.")
    timeout_seconds: float = Field(default=10, gt=0, le=30)
    fail_on_error: bool = Field(default=True, description="Treat 4xx/5xx responses as a node failure.")


class HTTPResult(BaseModel):
    status_code: int
    ok: bool
    url: str
    headers: dict[str, str]
    body: Any
    truncated: bool
    elapsed_ms: int


def _parse_body(response: httpx.Response) -> tuple[Any, bool]:
    if "json" in response.headers.get("content-type", ""):
        try:
            return response.json(), False
        except ValueError:
            pass
    text = response.text
    if len(text) > MAX_BODY_CHARS:
        return text[:MAX_BODY_CHARS], True
    return text, False


@register_node("http_request")
class HTTPRequestNode(NodeDefinition[HTTPRequestConfig]):
    category = "integration"
    label = "HTTP Request"
    description = "Calls a public HTTP endpoint and exposes the status, headers, and parsed body."
    icon = "globe"
    config_schema = HTTPRequestConfig
    output_schema = HTTPResult

    async def execute(self, context: NodeContext, config: HTTPRequestConfig) -> NodeResult:
        if not config.url.startswith(("http://", "https://")):
            return NodeResult.fail(f"URL must start with http:// or https:// (got '{config.url}')")

        body_kwargs: dict[str, Any] = {}
        if isinstance(config.body, (dict, list)):
            body_kwargs["json"] = config.body
        elif config.body is not None:
            body_kwargs["content"] = str(config.body)

        # SSRF guard: unless private networks are allowed, every connection (redirects
        # included) must go to a public address. An injected test transport is still
        # subject to the up-front URL check.
        transport = context.services.http_transport
        guarded = not context.services.allow_private_network
        start = time.perf_counter()
        try:
            if guarded:
                check_url(config.url)
                transport = transport or guarded_transport()
            async with httpx.AsyncClient(
                timeout=config.timeout_seconds,
                follow_redirects=True,
                transport=transport,
                trust_env=not guarded,
            ) as client:
                response = await client.request(
                    config.method,
                    config.url,
                    headers=config.headers,
                    params=config.query or None,
                    **body_kwargs,
                )
        except BlockedDestination as exc:
            return NodeResult.fail(str(exc))
        except httpx.InvalidURL as exc:
            return NodeResult.fail(f"Invalid URL '{config.url}': {exc}")
        except httpx.HTTPError as exc:
            return NodeResult.fail(f"Request to {config.url} failed: {type(exc).__name__}: {exc}")

        body, truncated = _parse_body(response)
        output = {
            "status_code": response.status_code,
            "ok": response.is_success,
            "url": str(response.url),
            "headers": dict(response.headers),
            "body": body,
            "truncated": truncated,
            "elapsed_ms": round((time.perf_counter() - start) * 1000),
        }
        if config.fail_on_error and response.status_code >= 400:
            return NodeResult.fail(f"HTTP {response.status_code} from {config.method} {config.url}", **output)
        return NodeResult(success=True, output=output)
