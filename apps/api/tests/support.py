"""Helpers shared by the async-execution, WebSocket, and worker tests."""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

from app.services.events import events_channel


def delay_graph(seconds: float = 30) -> dict[str, Any]:
    """Text -> Delay -> Output."""
    return {
        "nodes": [
            {"id": "start", "type": "text", "config": {"text": "hello"}},
            {"id": "wait", "type": "delay", "config": {"seconds": seconds}},
            {"id": "out", "type": "output", "config": {"value": "{{start.text}}"}},
        ],
        "edges": [{"source": "start", "target": "wait"}, {"source": "wait", "target": "out"}],
    }


async def create_workflow(client, user, graph: dict[str, Any], name: str = "Async") -> str:
    wid = (await client.post("/api/workflows", json={"name": name}, headers=user.headers)).json()["id"]
    response = await client.put(f"/api/workflows/{wid}", json={"graph": graph}, headers=user.headers)
    assert response.status_code == 200, response.text
    return wid


async def queue_run(client, user, wid: str, inputs: dict[str, Any] | None = None) -> uuid.UUID:
    response = await client.post(f"/api/workflows/{wid}/run", json={"inputs": inputs or {}}, headers=user.headers)
    assert response.status_code == 202, response.text
    return uuid.UUID(response.json()["execution_id"])


class EventCollector:
    """Subscribes to an execution's channel; `events` fills as messages are read."""

    def __init__(self, pubsub):
        self.pubsub = pubsub
        self.events: list[dict[str, Any]] = []

    async def next(self, timeout: float = 5) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            message = await self.pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message and message["type"] == "message":
                event = json.loads(message["data"])
                self.events.append(event)
                return event
        raise TimeoutError("no execution event arrived")

    async def until(self, event_type: str, node_key: str | None = None, timeout: float = 5) -> dict[str, Any]:
        while True:
            event = await self.next(timeout)
            if event["type"] == event_type and (node_key is None or event.get("node_key") == node_key):
                return event

    async def drain(self, quiet: float = 0.3) -> list[dict[str, Any]]:
        try:
            while True:
                await self.next(timeout=quiet)
        except TimeoutError:
            return self.events


@asynccontextmanager
async def collect_events(redis, execution_id):
    pubsub = redis.pubsub()
    await pubsub.subscribe(events_channel(execution_id))
    await pubsub.get_message(timeout=1)  # the subscribe confirmation
    collector = EventCollector(pubsub)
    try:
        yield collector
    finally:
        await pubsub.unsubscribe()
        await pubsub.aclose()


def kinds(events: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    return [(e["type"], e.get("node_key")) for e in events]
