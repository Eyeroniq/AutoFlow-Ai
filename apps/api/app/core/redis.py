"""Redis clients. The API shares one (lazily created on its event loop); worker tasks each
create their own because every task runs in a fresh event loop."""

from redis.asyncio import Redis

from app.core.config import settings

_client: Redis | None = None


def new_redis(url: str | None = None) -> Redis:
    return Redis.from_url(
        url or settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_keepalive=True,
        health_check_interval=30,
        retry_on_timeout=True,
    )


def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = new_redis()
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
