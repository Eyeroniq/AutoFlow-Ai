"""Downloading a URL for the RSS and Web Page nodes, behind the same SSRF guard as the HTTP
Request node (flowforge_engine.netguard): unless private networks are allowed, the URL and
every connection (redirects included) must reach a public address. Bodies are capped."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from flowforge_engine.netguard import BlockedDestination, check_url, guarded_transport
from flowforge_engine.services import ExecutionServices

# Some sites refuse clients without a browser-like User-Agent.
USER_AGENT = "Mozilla/5.0 (compatible; FlowForgeBot/1.0; +https://github.com/flowforge-ai)"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024


class FetchError(Exception):
    """The download failed; the message is safe to show as the node's error."""


@dataclass(frozen=True)
class Fetched:
    url: str
    status_code: int
    content_type: str
    content: bytes
    truncated: bool
    encoding: str | None

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")


async def fetch_url(
    services: ExecutionServices,
    url: str,
    *,
    timeout: float,
    accept: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Fetched:
    """GET `url` (following redirects). Raises FetchError for blocked, unreachable, or
    non-2xx responses."""
    if not url.startswith(("http://", "https://")):
        raise FetchError(f"URL must start with http:// or https:// (got '{url}')")
    transport = services.http_transport
    guarded = not services.allow_private_network
    try:
        if guarded:
            check_url(url)
            transport = transport or guarded_transport()
        async with (
            httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                transport=transport,
                trust_env=not guarded,
                headers={"User-Agent": USER_AGENT, "Accept": accept},
            ) as client,
            client.stream("GET", url) as response,
        ):
            chunks: list[bytes] = []
            size, truncated = 0, False
            async for chunk in response.aiter_bytes():
                if size + len(chunk) > max_bytes:
                    chunks.append(chunk[: max_bytes - size])
                    truncated = True
                    break
                chunks.append(chunk)
                size += len(chunk)
            if response.status_code >= 400:
                raise FetchError(f"HTTP {response.status_code} from {url}")
            return Fetched(
                url=str(response.url),
                status_code=response.status_code,
                content_type=response.headers.get("content-type", "").split(";")[0].strip().lower(),
                content=b"".join(chunks),
                truncated=truncated,
                encoding=response.charset_encoding,
            )
    except BlockedDestination as exc:
        raise FetchError(str(exc)) from None
    except httpx.InvalidURL as exc:
        raise FetchError(f"Invalid URL '{url}': {exc}") from None
    except httpx.TimeoutException:
        raise FetchError(f"{url} didn't answer within {timeout:g}s") from None
    except httpx.HTTPError as exc:
        raise FetchError(f"Request to {url} failed: {type(exc).__name__}: {exc}") from None
