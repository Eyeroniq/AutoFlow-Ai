"""Deterministic stand-ins. Used by the pytest suite (TESTING=true) and when a node
explicitly selects provider "mock" — never as a silent fallback for missing credentials."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import uuid
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from flowforge_engine.providers.base import MailboxQuery, OutgoingEmail

logger = logging.getLogger(__name__)


class MockLLMProvider:
    is_mock = True
    EMBEDDING_DIMENSIONS = 16

    def __init__(self, name: str = "mock"):
        self.name = name

    @staticmethod
    def response_for(user_prompt: str) -> str:
        return f"[MOCK RESPONSE to: {user_prompt[:50]}]"

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        return self.response_for(user_prompt)

    async def generate_with_image(
        self, system_prompt: str, user_prompt: str, image: bytes, mime_type: str, model: str, temperature: float, max_tokens: int,
    ) -> str:
        return self.response_for(f"[image {mime_type}, {len(image)} bytes] {user_prompt}")

    async def stream(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        words = self.response_for(user_prompt).split(" ")
        for index, word in enumerate(words):
            yield word if index == len(words) - 1 else word + " "

    async def embed(
        self, text: str, model: str | None = None, dimensions: int | None = None, task: str | None = None
    ) -> list[float]:
        """A hashed bag of words, normalized to unit length: stable across runs, and texts
        sharing words point the same way, so retrieval works without any key."""
        size = dimensions or self.EMBEDDING_DIMENSIONS
        raw = [0.0] * size
        words = re.findall(r"\w+", text.lower()) or [text]
        for word in words:
            digest = hashlib.sha256(word.encode("utf-8")).digest()
            raw[int.from_bytes(digest[:4], "big") % size] += 1.0 if digest[4] & 1 else -1.0
        if not any(raw):  # words cancelled out: fall back to the whole text's hash
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            raw[int.from_bytes(digest[:4], "big") % size] = 1.0
        norm = math.sqrt(sum(v * v for v in raw))
        return [round(v / norm, 6) for v in raw]

    async def embed_many(
        self, texts: list[str], model: str | None = None, dimensions: int | None = None, task: str | None = None
    ) -> list[list[float]]:
        return [await self.embed(text, model, dimensions, task) for text in texts]

    async def verify(self, model: str | None = None) -> dict[str, Any]:
        return {"mock": True, "model": model or "mock"}


@dataclass(frozen=True)
class SentEmail:
    message_id: str
    provider: str
    to: list[str]
    cc: list[str]
    subject: str
    body: str
    sent_at: str
    bcc: list[str] = field(default_factory=list)
    html_body: str | None = None
    attachments: list[str] = field(default_factory=list)
    read: bool = False


class MockEmailProvider:
    """Sends nothing: logs the email and records it in a process-wide, bounded outbox.

    It doubles as a mock mailbox: fetch_emails() reads back the outbox, so a mock
    Gmail → Gmail Read chain behaves plausibly in tests.
    """

    is_mock = True
    _outbox: ClassVar[deque[SentEmail]] = deque(maxlen=200)

    def __init__(self, name: str = "gmail"):
        self.name = name

    async def send_email(self, email: OutgoingEmail) -> dict[str, Any]:
        sent = SentEmail(
            message_id=f"mock-{uuid.uuid4().hex[:16]}",
            provider=self.name,
            to=list(email.to),
            cc=list(email.cc),
            bcc=list(email.bcc),
            subject=email.subject,
            body=email.body,
            html_body=email.html_body,
            attachments=[a.filename for a in email.attachments],
            sent_at=datetime.now(UTC).isoformat(),
        )
        self._outbox.append(sent)
        logger.info(
            "mock email sent",
            extra={"provider": self.name, "message_id": sent.message_id, "to": sent.to, "subject": sent.subject},
        )
        return {
            "message_id": sent.message_id,
            "status": "sent",
            "provider": self.name,
            "from": "mock@flowforge.local",
            "to": sent.to,
            "cc": sent.cc,
            "bcc": sent.bcc,
            "subject": sent.subject,
            "attachments": sent.attachments,
            "sent_at": sent.sent_at,
            "mock": True,
        }

    async def mailbox_status(self, folder: str = "INBOX") -> dict[str, Any]:
        # Mock UIDs are message ids, not numbers: polling relies on Message-ID de-duplication.
        return {"uidvalidity": 1, "uidnext": 1, "messages": len(self._outbox), "mock": True}

    async def fetch_emails(self, query: MailboxQuery) -> list[dict[str, Any]]:
        matches = []
        for sent in reversed(self._outbox):
            if query.unread_only and sent.read:
                continue
            if query.subject and query.subject.lower() not in sent.subject.lower():
                continue
            if query.from_address and query.from_address.lower() not in "mock@flowforge.local":
                continue
            body = sent.body if query.include_body else ""
            matches.append({
                "uid": sent.message_id,
                "message_id": sent.message_id,
                "from": "mock@flowforge.local",
                "from_address": "mock@flowforge.local",
                "to": sent.to,
                "cc": sent.cc,
                "subject": sent.subject,
                "date": sent.sent_at,
                "unread": not sent.read,
                "snippet": sent.body[:200],
                "body_text": body[: query.max_body_chars],
                "body_truncated": len(body) > query.max_body_chars,
                "attachments": [{"filename": name, "content_type": None, "size": None} for name in sent.attachments],
                "size": len(sent.body),
            })
            if len(matches) >= query.max_results:
                break
        return matches

    async def verify(self) -> dict[str, Any]:
        return {"mock": True}

    @classmethod
    def outbox(cls) -> list[SentEmail]:
        return list(cls._outbox)

    @classmethod
    def clear_outbox(cls) -> None:
        cls._outbox.clear()


@dataclass(frozen=True)
class SentMessage:
    provider: str
    destination: str
    payload: dict[str, Any]
    message_id: str
    sent_at: str


class _MockMessenger:
    """Records messages in a process-wide, bounded outbox instead of sending them."""

    is_mock = True
    _outbox: ClassVar[deque[SentMessage]]

    def __init__(self, name: str):
        self.name = name

    def _record(self, destination: str, payload: dict[str, Any]) -> SentMessage:
        sent = SentMessage(
            provider=self.name, destination=destination, payload=dict(payload),
            message_id=str(len(self._outbox) + 1), sent_at=datetime.now(UTC).isoformat(),
        )
        self._outbox.append(sent)
        logger.info("mock message sent", extra={"provider": self.name, "destination": destination})
        return sent

    @classmethod
    def outbox(cls) -> list[SentMessage]:
        return list(cls._outbox)

    @classmethod
    def clear_outbox(cls) -> None:
        cls._outbox.clear()


class MockTelegramProvider(_MockMessenger):
    _outbox: ClassVar[deque[SentMessage]] = deque(maxlen=200)

    def __init__(self, name: str = "telegram"):
        super().__init__(name)

    async def send_message(self, chat_id: str, text: str, **options: Any) -> dict[str, Any]:
        sent = self._record(str(chat_id), {"text": text, **options})
        return {"message_id": int(sent.message_id), "chat": {"id": chat_id}, "date": 0, "text": text}

    async def verify(self, chat_id: str | None = None) -> dict[str, Any]:
        return {"mock": True, "bot": "@mock_bot", "chat": {"id": chat_id} if chat_id else None}


class MockDiscordProvider(_MockMessenger):
    _outbox: ClassVar[deque[SentMessage]] = deque(maxlen=200)

    def __init__(self, name: str = "discord"):
        super().__init__(name)

    async def execute(self, payload: dict[str, Any], *, thread_id: str | None = None) -> dict[str, Any]:
        sent = self._record(thread_id or "webhook", payload)
        return {"id": sent.message_id, "channel_id": "mock", **payload}

    async def verify(self) -> dict[str, Any]:
        return {"mock": True, "webhook": "mock"}
