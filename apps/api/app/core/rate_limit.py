"""The one rate limiter: a sliding window kept in Redis, shared by every API process and worker.

Used by the auth endpoints, workflow runs, deployment runs and status polling, and the Telegram
Command Center. Nothing is counted in process memory, so limits hold across restarts, multiple API
replicas, and the listener service.

A limit is written "10/minute" (second, minute, hour, day). Each limited scope and key
(an IP address, a deployment, a user, a chat) has a sorted set of recent hit times; one Lua script
drops hits older than the window, then admits the hit if fewer than `count` remain. It runs
atomically in Redis, so concurrent requests can't both take the last slot. A refused request is not
counted (a client that backs off recovers), but it is tallied separately so callers can react to the
first refusal in a window (the Telegram bot says "slow down" once).

If Redis is unreachable the limiter fails open and logs it: an outage of the cache must not take the
API down with it (the run queue needs Redis anyway, so runs fail loudly on their own).

    @router.post("/login")
    @limiter.limit(settings.AUTH_RATE_LIMIT)                    # per client IP
    async def login(request: Request, ...): ...

    @limiter.limit("30/minute", key_func=per_user)              # per signed-in user
"""

import functools
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import redis as sync_redis
from fastapi import Request
from jose import JWTError, jwt
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

PREFIX = "flowforge:ratelimit"
UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
_LIMIT = re.compile(r"^\s*(\d+)\s*(?:/|per)\s*(\d*)\s*(second|minute|hour|day)s?\s*$", re.I)

# KEYS[1] hits (sorted set of timestamps), KEYS[2] refusals in this window
# ARGV: now (ms), window (ms), count, unique member
_SCRIPT = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
local used = redis.call('ZCARD', KEYS[1])
if used < limit then
  redis.call('ZADD', KEYS[1], now, ARGV[4])
  redis.call('PEXPIRE', KEYS[1], window)
  return {1, limit - used - 1, 0, 0}
end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
local retry = window - (now - tonumber(oldest[2]))
local refused = redis.call('INCR', KEYS[2])
if refused == 1 then redis.call('PEXPIRE', KEYS[2], window) end
return {0, 0, retry, refused}
"""


@dataclass(frozen=True)
class Limit:
    count: int
    seconds: int

    @classmethod
    def parse(cls, text: str) -> "Limit":
        match = _LIMIT.match(text)
        if not match:
            raise ValueError(f"bad rate limit {text!r}; write it like '10/minute'")
        count, multiple, unit = int(match.group(1)), int(match.group(2) or 1), match.group(3).lower()
        return cls(count, multiple * UNITS[unit])

    def __str__(self) -> str:
        for unit, seconds in sorted(UNITS.items(), key=lambda kv: -kv[1]):
            if self.seconds % seconds == 0:
                n = self.seconds // seconds
                return f"{self.count}/{unit}" if n == 1 else f"{self.count} per {n} {unit}s"
        return f"{self.count} per {self.seconds} seconds"


@dataclass(frozen=True)
class Hit:
    allowed: bool
    remaining: int
    retry_after: float  # seconds until a slot frees up (0 when allowed)
    refusals: int  # refused hits in this window, this one included (0 when allowed)


class RateLimitExceeded(Exception):
    def __init__(self, limit: Limit, retry_after: float):
        self.limit, self.retry_after = limit, retry_after
        self.detail = str(limit)
        super().__init__(f"rate limit exceeded: {limit}")


def client_ip(request: Request) -> str:
    """The caller's address. Behind the Caddy proxy that is X-Forwarded-For's first hop, which Caddy
    sets itself (it overwrites anything a client sent); without a proxy it is the socket peer."""
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def per_user(request: Request) -> str:
    """The signed-in user (from the bearer token's `sub`), else the client address. The signature is
    checked; whether the user still exists is left to the route's own auth."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        try:
            claims = jwt.decode(header[7:], settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
            if claims.get("sub"):
                return f"user:{claims['sub']}"
        except JWTError:
            pass
    return f"ip:{client_ip(request)}"


class RateLimiter:
    def __init__(self) -> None:
        self.enabled = True

    async def hit(self, scope: str, key: str, limit: Limit | str, *, redis: Redis | None = None) -> Hit:
        """Count one request for (`scope`, `key`) against `limit`."""
        limit = Limit.parse(limit) if isinstance(limit, str) else limit
        if not self.enabled:
            return Hit(True, limit.count, 0, 0)
        base = f"{PREFIX}:{scope}:{key}:{limit.count}:{limit.seconds}"
        try:
            allowed, remaining, retry_ms, refusals = await (redis or get_redis()).eval(
                _SCRIPT, 2, base, f"{base}:refused", int(time.time() * 1000), limit.seconds * 1000, limit.count, uuid.uuid4().hex
            )
        except (RedisError, OSError) as exc:
            logger.warning("rate limiter can't reach Redis; letting the request through", extra={"error": str(exc), "scope": scope})
            return Hit(True, limit.count, 0, 0)
        return Hit(bool(allowed), int(remaining), int(retry_ms) / 1000, int(refusals))

    async def check(self, scope: str, key: str, limit: Limit | str, *, redis: Redis | None = None) -> None:
        """Like `hit`, raising RateLimitExceeded when refused."""
        parsed = Limit.parse(limit) if isinstance(limit, str) else limit
        result = await self.hit(scope, key, parsed, redis=redis)
        if not result.allowed:
            raise RateLimitExceeded(parsed, result.retry_after)

    def limit(self, value: str | Callable[[], str], key_func: Callable[[Request], str] = client_ip) -> Callable[..., Any]:
        """Decorator for a route function that takes `request: Request`. `value` may be a callable so the
        limit is read per request (settings, tests)."""

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            scope = f"{func.__module__}.{func.__qualname__}"

            @functools.wraps(func)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                request = next((a for a in (*args, *kwargs.values()) if isinstance(a, Request)), None)
                if request is not None and self.enabled:
                    await self.check(scope, key_func(request), value() if callable(value) else value)
                return await func(*args, **kwargs)

            return wrapper

        return decorator

    def reset(self) -> None:
        """Forget every counter (tests). Synchronous on purpose, so a fixture can call it."""
        client = sync_redis.Redis.from_url(settings.REDIS_URL)
        try:
            for key in client.scan_iter(f"{PREFIX}:*"):
                client.delete(key)
        finally:
            client.close()


limiter = RateLimiter()
