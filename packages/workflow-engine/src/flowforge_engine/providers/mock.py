from __future__ import annotations

import hashlib
import logging
import math
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


class MockLLMProvider:
    """Deterministic stand-in used when no API key is configured (and in tests)."""

    is_mock = True
    EMBEDDING_DIMENSIONS = 16

    def __init__(self, name: str = "mock"):
        self.name = name

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        return f"[MOCK RESPONSE to: {user_prompt[:50]}]"

    async def embed(self, text: str) -> list[float]:
        # Stable across runs: derived from a hash of the text, normalized to unit length.
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [(byte / 127.5) - 1.0 for byte in digest[: self.EMBEDDING_DIMENSIONS]]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [round(v / norm, 6) for v in raw]


@dataclass(frozen=True)
class SentEmail:
    message_id: str
    provider: str
    to: list[str]
    cc: list[str]
    subject: str
    body: str
    sent_at: str


class MockEmailProvider:
    """Sends nothing: logs the email and records it in a process-wide, bounded outbox."""

    is_mock = True
    _outbox: ClassVar[deque[SentEmail]] = deque(maxlen=200)

    def __init__(self, name: str = "gmail"):
        self.name = name

    async def send_email(
        self, to: list[str], cc: list[str], subject: str, body: str
    ) -> dict[str, Any]:
        email = SentEmail(
            message_id=f"mock-{uuid.uuid4().hex[:16]}",
            provider=self.name,
            to=list(to),
            cc=list(cc),
            subject=subject,
            body=body,
            sent_at=datetime.now(UTC).isoformat(),
        )
        self._outbox.append(email)
        logger.info(
            "mock email sent",
            extra={"provider": self.name, "message_id": email.message_id, "to": email.to, "subject": subject},
        )
        receipt = asdict(email)
        del receipt["body"]
        return {**receipt, "status": "sent", "mock": True}

    @classmethod
    def outbox(cls) -> list[SentEmail]:
        return list(cls._outbox)

    @classmethod
    def clear_outbox(cls) -> None:
        cls._outbox.clear()
