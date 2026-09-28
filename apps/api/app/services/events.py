"""Execution events over Redis pub/sub, plus the stop flag.

Every event goes to channel `flowforge:execution:<id>:events` as one JSON object:

    {"seq": 7, "type": "node.succeeded", "execution_id": "...", "timestamp": "...Z", "node_key": "gemini", ...}

`seq` increases by one per event of an execution, across every publisher (worker, API),
because a Lua script assigns it and publishes in one atomic step. The WebSocket uses it
to merge its database snapshot with the live stream without gaps or duplicates.
Publishing is best effort: the database is the source of truth, so a Redis hiccup is
logged and the run carries on.
"""

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

KEY_TTL_SECONDS = 7 * 24 * 3600

EVENT_TYPES = (
    "execution.started",
    "node.started",
    "node.token",
    "node.succeeded",
    "node.failed",
    "node.skipped",
    "execution.finished",
)

# KEYS[1] = seq key, KEYS[2] = channel; ARGV[1] = event JSON without "seq", ARGV[2] = TTL.
# The body always starts with '{"type":', so '{"seq":N,' + body[2:] stays valid JSON.
_PUBLISH_SCRIPT = """
local seq = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
redis.call('PUBLISH', KEYS[2], '{"seq":' .. seq .. ',' .. string.sub(ARGV[1], 2))
return seq
"""


def events_channel(execution_id: uuid.UUID | str) -> str:
    return f"flowforge:execution:{execution_id}:events"


def seq_key(execution_id: uuid.UUID | str) -> str:
    return f"flowforge:execution:{execution_id}:seq"


def stop_key(execution_id: uuid.UUID | str) -> str:
    return f"flowforge:execution:{execution_id}:stop"


def iso(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat() if moment else None


def event_body(event_type: str, execution_id: uuid.UUID | str, **fields: Any) -> dict[str, Any]:
    return {
        "type": event_type,
        "execution_id": str(execution_id),
        "timestamp": datetime.now(UTC).isoformat(),
        **fields,
    }


class EventPublisher:
    def __init__(self, redis: Redis):
        self._redis = redis
        self._script = redis.register_script(_PUBLISH_SCRIPT)

    async def publish(self, execution_id: uuid.UUID | str, event_type: str, **fields: Any) -> int | None:
        """Publish one event; returns its seq, or None if Redis was unreachable."""
        body = json.dumps(event_body(event_type, execution_id, **fields), default=str, separators=(",", ":"))
        try:
            return int(await self._script(keys=[seq_key(execution_id), events_channel(execution_id)],
                                          args=[body, KEY_TTL_SECONDS]))
        except RedisError as exc:
            logger.warning(
                "could not publish execution event",
                extra={"execution_id": str(execution_id), "event_type": event_type, "error": str(exc)},
            )
            return None


async def current_seq(redis: Redis, execution_id: uuid.UUID | str) -> int:
    value = await redis.get(seq_key(execution_id))
    return int(value) if value else 0


async def set_stop_flag(redis: Redis, execution_id: uuid.UUID | str) -> None:
    await redis.set(stop_key(execution_id), "1", ex=KEY_TTL_SECONDS)


async def stop_flag_set(redis: Redis, execution_id: uuid.UUID | str) -> bool:
    return bool(await redis.exists(stop_key(execution_id)))
