"""Deterministic stand-ins. Used by the pytest suite (TESTING=true) and when a node
explicitly selects provider "mock" — never as a silent fallback for missing credentials."""

from __future__ import annotations

import hashlib
import logging
import math
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

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        # Stable across runs: derived from a hash of the text, normalized to unit length.
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [(byte / 127.5) - 1.0 for byte in digest[: self.EMBEDDING_DIMENSIONS]]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [round(v / norm, 6) for v in raw]

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
