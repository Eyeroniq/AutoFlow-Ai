from __future__ import annotations

import base64
import binascii
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

# Gmail rejects messages over 25 MB; base64 inflates attachments by a third.
MAX_ATTACHMENT_BYTES = 18 * 1024 * 1024


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    is_mock: bool

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str: ...

    def stream(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """Yield the response text as it is generated (an async generator)."""
        ...

    async def embed(
        self, text: str, model: str | None = None, dimensions: int | None = None, task: str | None = None
    ) -> list[float]:
        """Raises ProviderNotSupportedError when the provider has no embeddings API.

        `dimensions` asks for a shorter vector where the model supports it; `task` is
        "document" or "query" for providers with retrieval task types. Providers may also
        offer `embed_many(texts, model, dimensions, task)` for one request per batch
        (flowforge_engine.knowledge.embed_texts uses it when present)."""
        ...

    async def verify(self, model: str | None = None) -> dict[str, Any]:
        """A cheap real call (list/get models) proving the credentials work."""
        ...


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    content: str
    encoding: Literal["text", "base64"] = "text"
    content_type: str | None = None

    def data(self) -> bytes:
        if self.encoding == "text":
            return self.content.encode("utf-8")
        try:
            return base64.b64decode(self.content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"attachment '{self.filename}' is not valid base64") from exc


@dataclass(frozen=True)
class OutgoingEmail:
    to: list[str]
    subject: str
    body: str
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    html_body: str | None = None
    attachments: list[EmailAttachment] = field(default_factory=list)


@dataclass(frozen=True)
class MailboxQuery:
    folder: str = "INBOX"
    from_address: str | None = None
    subject: str | None = None
    unread_only: bool = True
    since_days: int | None = None
    max_results: int = 10
    mark_as_read: bool = False
    include_body: bool = True
    max_body_chars: int = 5000
    # Polling (the email trigger): only messages with a UID above this, and the oldest
    # `max_results` of them instead of the newest, so a backlog is worked through in order.
    uid_after: int | None = None
    oldest_first: bool = False


@runtime_checkable
class EmailProvider(Protocol):
    name: str
    is_mock: bool

    async def send_email(self, email: OutgoingEmail) -> dict[str, Any]: ...

    async def verify(self) -> dict[str, Any]: ...


@runtime_checkable
class MailboxProvider(Protocol):
    name: str
    is_mock: bool

    async def fetch_emails(self, query: MailboxQuery) -> list[dict[str, Any]]: ...

    async def mailbox_status(self, folder: str = "INBOX") -> dict[str, Any]:
        """{"uidvalidity": int, "uidnext": int, "messages": int} for a folder. A new
        UIDVALIDITY means earlier UIDs no longer identify the same messages."""
        ...

    async def verify(self) -> dict[str, Any]: ...
