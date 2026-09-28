"""WS /ws/executions/{execution_id}: an execution's state, then its live events.

Protocol (server -> client, one JSON object per message):

1. Authenticate: `?token=<access token>` in the URL, or -- keeping the token out of URLs
   and proxy logs -- send `{"type": "auth", "token": "<access token>"}` as the first
   message within WS_AUTH_TIMEOUT_SECONDS. Failures close with 4401; an execution that
   doesn't exist or isn't yours closes with 4404 (the REST API's 404 convention).
2. `{"type": "snapshot", "seq": N, "execution": {...}}` -- the full state from the
   database (same shape as GET /api/executions/{id}), so late joiners see everything.
3. Live events (`execution.started`, `node.started`, `node.token`, `node.succeeded`,
   `node.failed`, `node.skipped`, `execution.finished`), only those with seq > N.
4. After `execution.finished` the server closes with 1000. Joining a finished execution
   yields the snapshot, a replayed `execution.finished` ("replayed": true), and the close.

The server sends `{"type": "heartbeat"}` every WS_HEARTBEAT_SECONDS and answers
`{"type": "ping"}` with `{"type": "pong"}`. If Redis drops, it reconnects and sends a fresh
snapshot with `"resync": true`; the database is also re-checked every WS_DB_CHECK_SECONDS.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError
from sqlalchemy import select
from starlette.websockets import WebSocketState

from app.api.deps import SessionFactoryDep, user_from_token
from app.core.config import settings
from app.core.redis import get_redis
from app.core.security import InvalidTokenError
from app.db.session import SessionFactory
from app.models.execution import WorkflowExecution
from app.models.user import User
from app.schemas.execution import ExecutionDetail
from app.services.events import current_seq, events_channel
from app.services.runs import TERMINAL_STATUSES, load_execution_detail, owned_execution

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

CLOSE_NORMAL = 1000
CLOSE_INTERNAL_ERROR = 1011
CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404


def _now() -> str:
    return datetime.now(UTC).isoformat()


def replayed_finished(detail: ExecutionDetail) -> dict[str, Any]:
    return {
        "type": "execution.finished",
        "execution_id": str(detail.id),
        "seq": None,
        "timestamp": _now(),
        "replayed": True,
        "status": detail.status.value,
        "final_output": detail.final_output,
        "error": detail.error_message,
        "started_at": detail.started_at.isoformat() if detail.started_at else None,
        "finished_at": detail.finished_at.isoformat() if detail.finished_at else None,
        "duration_ms": detail.duration_ms,
    }


@router.websocket("/ws/executions/{execution_id}")
async def execution_events(
    websocket: WebSocket, execution_id: uuid.UUID, session_factory: SessionFactoryDep, token: str | None = None
) -> None:
    await websocket.accept()
    stream = ExecutionStream(websocket, execution_id, session_factory, get_redis())
    try:
        await stream.run(token)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("execution websocket failed", extra={"execution_id": str(execution_id)})
        await stream.close(CLOSE_INTERNAL_ERROR, "Internal error")
    finally:
        await stream.unsubscribe()


class ExecutionStream:
    def __init__(self, websocket: WebSocket, execution_id: uuid.UUID, sessions: SessionFactory, redis: Redis):
        self.ws = websocket
        self.execution_id = execution_id
        self.sessions = sessions
        self.redis = redis
        self.channel = events_channel(execution_id)
        self.user: User | None = None
        self.pubsub: PubSub | None = None
        self.seen_seq = 0
        self._send_lock = asyncio.Lock()

    # --- plumbing ----------------------------------------------------------------------

    @property
    def open(self) -> bool:
        return (self.ws.application_state == WebSocketState.CONNECTED
                and self.ws.client_state == WebSocketState.CONNECTED)

    async def send(self, message: dict[str, Any] | str) -> None:
        text = message if isinstance(message, str) else json.dumps(message, default=str)
        async with self._send_lock:
            if self.open:
                await self.ws.send_text(text)

    async def close(self, code: int, reason: str = "") -> None:
        async with self._send_lock:
            if self.open:
                with contextlib.suppress(RuntimeError):
                    await self.ws.close(code=code, reason=reason.encode()[:120].decode(errors="ignore"))

    async def reject(self, code: int, message: str) -> None:
        await self.send({"type": "error", "code": code, "message": message})
        await self.close(code, message)

    async def subscribe(self) -> bool:
        try:
            pubsub = self.redis.pubsub()
            await pubsub.subscribe(self.channel)
        except (RedisError, OSError) as exc:
            logger.warning("can't subscribe to execution events", extra={"execution_id": str(self.execution_id), "error": str(exc)})
            return False
        self.pubsub = pubsub
        return True

    async def unsubscribe(self) -> None:
        pubsub, self.pubsub = self.pubsub, None
        if pubsub is not None:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe()
                await pubsub.aclose()

    # --- protocol ----------------------------------------------------------------------

    async def run(self, token: str | None) -> None:
        user = await self.authenticate(token)
        if user is None:
            return
        async with self.sessions() as db:
            owned = await owned_execution(db, self.execution_id, user) is not None
        if not owned:
            await self.reject(CLOSE_NOT_FOUND, "Execution not found")
            return
        self.user = user

        # Subscribe before reading the snapshot so nothing published in between is lost.
        await self.subscribe()
        if await self.send_snapshot():
            return

        tasks = [
            asyncio.create_task(self.forward_events()),
            asyncio.create_task(self.receive_messages()),
            asyncio.create_task(self.heartbeat()),
            asyncio.create_task(self.watch_database()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                error = task.exception()
                if error is not None and not isinstance(error, WebSocketDisconnect):
                    raise error
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.close(CLOSE_NORMAL)

    async def authenticate(self, token: str | None) -> User | None:
        if token is None:
            try:
                message = await asyncio.wait_for(self.ws.receive(), timeout=settings.WS_AUTH_TIMEOUT_SECONDS)
            except TimeoutError:
                await self.reject(CLOSE_UNAUTHORIZED, (
                    'Not authenticated: pass ?token=<access token> or send {"type": "auth", "token": ...} first'
                ))
                return None
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", CLOSE_NORMAL))
            try:
                payload = json.loads(message.get("text") or "")
            except ValueError:
                payload = None
            if not (isinstance(payload, dict) and payload.get("type") == "auth" and isinstance(payload.get("token"), str)):
                await self.reject(CLOSE_UNAUTHORIZED, 'First message must be {"type": "auth", "token": "<access token>"}')
                return None
            token = payload["token"]
        try:
            async with self.sessions() as db:
                return await user_from_token(db, token)
        except InvalidTokenError as exc:
            await self.reject(CLOSE_UNAUTHORIZED, str(exc))
            return None

    async def send_snapshot(self, *, resync: bool = False) -> bool:
        """Send the database state; if the execution is finished, also send the (replayed)
        execution.finished event and close. Returns True when the stream is over."""
        assert self.user is not None
        try:
            seq = await current_seq(self.redis, self.execution_id)
        except (RedisError, OSError):
            seq = self.seen_seq
        async with self.sessions() as db:
            detail = await load_execution_detail(db, self.execution_id, self.user)
        if detail is None:  # deleted meanwhile
            await self.reject(CLOSE_NOT_FOUND, "Execution not found")
            return True
        self.seen_seq = max(self.seen_seq, seq)
        await self.send({
            "type": "snapshot", "execution_id": str(self.execution_id), "seq": seq, "resync": resync,
            "timestamp": _now(), "execution": detail.model_dump(mode="json"),
        })
        if detail.status in TERMINAL_STATUSES:
            await self.send(replayed_finished(detail))
            await self.close(CLOSE_NORMAL)
            return True
        return False

    async def forward_events(self) -> None:
        backoff = 0.5
        while True:
            if self.pubsub is None:
                if not await self.subscribe():
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 10)
                    continue
                backoff = 0.5
                # Events may have been missed while disconnected: resync from the database.
                if await self.send_snapshot(resync=True):
                    return
            try:
                message = await self.pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            except (RedisError, OSError) as exc:
                logger.warning("execution events connection dropped; reconnecting",
                               extra={"execution_id": str(self.execution_id), "error": str(exc)})
                await self.unsubscribe()
                continue
            if not message or message.get("type") != "message":
                continue
            data = message["data"]
            try:
                event = json.loads(data)
            except ValueError:
                continue
            seq = event.get("seq") or 0
            if seq and seq <= self.seen_seq:
                continue  # already reflected in the snapshot
            self.seen_seq = max(self.seen_seq, seq)
            await self.send(data)
            if event.get("type") == "execution.finished":
                await self.close(CLOSE_NORMAL)
                return

    async def receive_messages(self) -> None:
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            try:
                payload = json.loads(message.get("text") or "")
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("type") == "ping":
                await self.send({"type": "pong", "timestamp": _now()})

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(settings.WS_HEARTBEAT_SECONDS)
            await self.send({"type": "heartbeat", "timestamp": _now()})

    async def watch_database(self) -> None:
        """Safety net for a missed execution.finished (e.g. published while Redis was down)."""
        while True:
            await asyncio.sleep(settings.WS_DB_CHECK_SECONDS)
            async with self.sessions() as db:
                status = await db.scalar(
                    select(WorkflowExecution.status).where(WorkflowExecution.id == self.execution_id)
                )
            if status in TERMINAL_STATUSES:
                await asyncio.sleep(1)  # give the live event a moment to arrive first
                if await self.send_snapshot(resync=True):
                    return
