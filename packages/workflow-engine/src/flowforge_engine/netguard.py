"""SSRF guard for outbound HTTP from workflows (the HTTP Request node).

A workflow author controls the URL, so by default requests may only reach public
addresses. The check runs where the connection is made, for every connection: the host
name is resolved, *every* address it resolves to must be public, and the socket is opened
to the address that was checked. So it holds for IP literals, names that resolve to
internal addresses (including DNS rebinding: there's no second lookup to race), and each
redirect hop (a new host means a new connection, which is checked again).

Blocked: loopback, private (10/8, 172.16/12, 192.168/16, fc00::/7), link-local
(169.254/16, fe80::/10, which includes cloud metadata at 169.254.169.254), CGNAT,
multicast, reserved and unspecified addresses, IPv4-mapped IPv6 forms of those, and
internal-looking names (localhost, *.internal, *.local, and single-label names such as the
Docker service names "postgres" or "redis") even before DNS. Environment proxies are
ignored, since a proxy would make the connection on our behalf.

`HTTP_ALLOW_PRIVATE_NETWORKS=true` turns the guard off, for local development only.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import httpcore
import httpx

ALLOW_ENV = "HTTP_ALLOW_PRIVATE_NETWORKS"
_HINT = f"Set {ALLOW_ENV}=true to allow internal addresses (local development only)."

_BLOCKED_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa", ".lan")

Resolver = Callable[[str, int], Awaitable[list[str]]]


class BlockedDestination(Exception):
    """The request would reach a private or internal address."""


def ip_block_reason(address: str) -> str | None:
    """Why this IP must not be contacted, or None if it's a public address."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        return "a link-local address"
    if ip.is_unspecified:
        return "an unspecified address"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_private:
        return "a private network address"
    if not ip.is_global:
        return "a reserved, non-public address"
    return None


def name_block_reason(host: str) -> str | None:
    """Why a host *name* is internal on its face (before any DNS), or None."""
    name = host.strip().rstrip(".").lower()
    if not name:
        return "an empty host name"
    if name in _BLOCKED_NAMES or name.endswith(_BLOCKED_SUFFIXES):
        return "an internal host name"
    if "." not in name:
        return "a single-label host name (e.g. a Docker service name)"
    return None


def _as_ip(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host.strip("[]").split("%", 1)[0]))
    except ValueError:
        return None


def check_url(url: str) -> None:
    """A quick check of the URL's host before any network I/O (names and IP literals).

    The authoritative check is at connect time (GuardedNetworkBackend); this one just
    fails fast with a clear message.
    """
    host = httpx.URL(url).host
    literal = _as_ip(host)
    reason = ip_block_reason(literal) if literal else name_block_reason(host)
    if reason:
        raise BlockedDestination(f"Blocked request to '{host}': it is {reason}. {_HINT}")


async def _system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class GuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolves, checks every address, then connects to the checked address."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None, *, resolver: Resolver | None = None):
        self._inner = inner or httpcore.AnyIOBackend()
        self._resolve = resolver or _system_resolve

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        literal = _as_ip(host)
        if literal is None:
            if reason := name_block_reason(host):
                raise BlockedDestination(f"Blocked request to '{host}': it is {reason}. {_HINT}")
            try:
                addresses = await self._resolve(host, port)
            except OSError as exc:
                raise httpcore.ConnectError(f"Could not resolve '{host}': {exc}") from exc
            if not addresses:
                raise httpcore.ConnectError(f"'{host}' did not resolve to any address")
        else:
            addresses = [literal]
        for address in addresses:
            if reason := ip_block_reason(address):
                target = f"'{host}'" if literal else f"'{host}' (resolves to {address})"
                raise BlockedDestination(f"Blocked request to {target}: it is {reason}. {_HINT}")
        # TLS still verifies against `host`: httpcore passes the original name as SNI.
        return await self._inner.connect_tcp(
            addresses[0], port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Iterable[Any] | None = None) -> httpcore.AsyncNetworkStream:
        raise BlockedDestination("Blocked request over a Unix socket")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def guarded_transport(*, resolver: Resolver | None = None) -> httpx.AsyncHTTPTransport:
    """An httpx transport whose every connection goes through GuardedNetworkBackend."""
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    pool = transport._pool
    if not hasattr(pool, "_network_backend"):  # pragma: no cover - guards an httpcore change
        raise RuntimeError("httpcore changed: can't install the SSRF guard on the connection pool")
    pool._network_backend = GuardedNetworkBackend(resolver=resolver)
    return transport
