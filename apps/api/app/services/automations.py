"""Pause and resume the always-on services from the UI, without stopping their containers.

A flag per service lives in Redis (`flowforge:paused:<name>`). The Telegram listener reads it on every poll (while paused it
fetches the messages and drops them, so nothing piles up to be replayed on resume), and the Discord controller reads it
on every watch sync (while paused it tells the recorder to watch nothing and to stop a recording in progress).
"""

from redis.asyncio import Redis

SERVICES = ("discord", "telegram")
KEY = "flowforge:paused:{name}"


async def is_paused(redis: Redis, name: str) -> bool:
    return bool(await redis.exists(KEY.format(name=name)))


async def set_paused(redis: Redis, name: str, paused: bool) -> None:
    if paused:
        await redis.set(KEY.format(name=name), "1")
    else:
        await redis.delete(KEY.format(name=name))
