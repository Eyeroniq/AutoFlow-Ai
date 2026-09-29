"""Reading email over IMAP (Gmail with an App Password by default).

imaplib is blocking, so every network call runs in a worker thread via asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import html
import imaplib
import re
import ssl
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any

from flowforge_engine.errors import ProviderError
from flowforge_engine.providers.base import MailboxQuery
from flowforge_engine.providers.retry import RetryPolicy, redact, with_retries
from flowforge_engine.providers.settings import EmailAccount
from flowforge_engine.providers.smtp_provider import APP_PASSWORD_HINT, is_gmail_host, login_password

# Messages above this are returned with headers only (no body download).
MAX_FULL_FETCH_BYTES = 10 * 1024 * 1024
SNIPPET_CHARS = 200

_FETCH_META = re.compile(rb"UID (\d+)|FLAGS \(([^)]*)\)|RFC822\.SIZE (\d+)")


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</(?:p|div|li|tr|h[1-6])>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def _search_criteria(query: MailboxQuery) -> tuple[list[str], bytes | None]:
    """IMAP SEARCH terms, plus at most one UTF-8 literal for a non-ASCII filter."""
    terms: list[str] = []
    literal: bytes | None = None
    if query.uid_after is not None:
        terms += ["UID", f"{query.uid_after + 1}:*"]
    if query.unread_only:
        terms.append("UNSEEN")
    if query.since_days:
        since = datetime.now(UTC) - timedelta(days=query.since_days)
        terms += ["SINCE", since.strftime("%d-%b-%Y")]
    for key, value in (("FROM", query.from_address), ("SUBJECT", query.subject)):
        if not value:
            continue
        if value.isascii():
            terms += [key, _quote(value)]
        elif literal is None:
            # imaplib sends `literal` after the last term, so the non-ASCII term goes last.
            literal = value.encode("utf-8")
            pending_key = key
        else:
            raise ValueError("only one of from_address/subject may contain non-ASCII characters")
    if literal is not None:
        terms.append(pending_key)
    return terms or ["ALL"], literal


def _addresses(message: EmailMessage, header: str) -> list[str]:
    return [addr for _, addr in getaddresses([str(v) for v in message.get_all(header, [])]) if addr]


def _parse_message(
    raw: bytes, uid: str, flags: str, size: int | None, query: MailboxQuery, body_fetched: bool
) -> dict[str, Any]:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(message, EmailMessage)
    sender = str(message.get("From", ""))
    try:
        date = parsedate_to_datetime(str(message["Date"])).isoformat() if message["Date"] else None
    except (TypeError, ValueError):
        date = None

    text = ""
    attachments: list[dict[str, Any]] = []
    if body_fetched:
        plain = message.get_body(preferencelist=("plain",))
        if plain is not None:
            text = plain.get_content()
        else:
            rich = message.get_body(preferencelist=("html",))
            text = _html_to_text(rich.get_content()) if rich is not None else ""
        for part in message.iter_attachments():
            payload = part.get_payload(decode=True) or b""
            attachments.append({
                "filename": part.get_filename(),
                "content_type": part.get_content_type(),
                "size": len(payload),
            })
    text = text.strip()
    body = text if query.include_body else ""
    return {
        "uid": uid,
        "message_id": str(message.get("Message-ID", "")) or None,
        "from": sender,
        "from_address": parseaddr(sender)[1] or None,
        "to": _addresses(message, "To"),
        "cc": _addresses(message, "Cc"),
        "subject": str(message.get("Subject", "")),
        "date": date,
        "unread": "\\Seen" not in flags,
        "snippet": " ".join(text.split())[:SNIPPET_CHARS],
        "body_text": body[: query.max_body_chars],
        "body_truncated": (not body_fetched) or len(body) > query.max_body_chars,
        "attachments": attachments,
        "size": size,
    }


def _fetch_parts(response: list[Any]) -> list[tuple[str, str, int | None, bytes]]:
    """(uid, flags, size, data) for each message in an imaplib FETCH response."""
    parts = []
    for item in response:
        if not isinstance(item, tuple):
            continue
        meta, data = item[0], item[1]
        uid, flags, size = "", "", None
        for match in _FETCH_META.finditer(meta):
            if match.group(1):
                uid = match.group(1).decode()
            elif match.group(2) is not None:
                flags = match.group(2).decode()
            elif match.group(3):
                size = int(match.group(3))
        parts.append((uid, flags, size, data))
    return parts


class IMAPEmailProvider:
    is_mock = False

    def __init__(
        self,
        account: EmailAccount,
        *,
        name: str = "gmail",
        retry: RetryPolicy | None = None,
        imap_factory: Callable[..., imaplib.IMAP4] | None = None,
    ):
        if not account.configured:
            raise ValueError("IMAPEmailProvider needs a username and password")
        self.name = name
        self.account = account
        self._retry = retry or RetryPolicy()
        self._factory = imap_factory

    def _text(self, raw: Any) -> str:
        if isinstance(raw, BaseException) and raw.args:
            raw = raw.args[0]  # imaplib errors carry the server's bytes reply
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        return redact(text, login_password(self.account))

    def _connect(self) -> imaplib.IMAP4:
        account = self.account
        try:
            if self._factory is not None:
                imap = self._factory(account.imap_host, account.imap_port, timeout=account.timeout_seconds)
            else:
                imap = imaplib.IMAP4_SSL(
                    account.imap_host, account.imap_port,
                    ssl_context=ssl.create_default_context(), timeout=account.timeout_seconds,
                )
        except (OSError, imaplib.IMAP4.error) as exc:
            raise ProviderError(
                self.name,
                f"cannot connect to {account.imap_host}:{account.imap_port}: {type(exc).__name__}: {exc}",
                retryable=True,
            ) from exc
        try:
            imap.login(account.username or "", login_password(account))
        except imaplib.IMAP4.abort as exc:
            raise ProviderError(self.name, f"IMAP connection dropped during login: {self._text(exc)}", retryable=True) from exc
        except imaplib.IMAP4.error as exc:
            hint = APP_PASSWORD_HINT if is_gmail_host(account.imap_host) else "Check the username and password"
            raise ProviderError(self.name, f"IMAP login rejected for {account.username} ({self._text(exc)}). {hint}") from exc
        return imap

    @staticmethod
    def _logout(imap: imaplib.IMAP4) -> None:
        try:
            imap.logout()
        except (OSError, imaplib.IMAP4.error):
            pass

    @staticmethod
    def _mailbox_name(folder: str) -> str:
        return _quote(folder) if re.search(r'[\s"\\()]', folder) else folder

    def _fetch_sync(self, query: MailboxQuery) -> list[dict[str, Any]]:
        terms, literal = _search_criteria(query)
        imap = self._connect()
        try:
            status, data = imap.select(self._mailbox_name(query.folder), readonly=not query.mark_as_read)
            if status != "OK":
                raise ProviderError(self.name, f"cannot open folder '{query.folder}': {self._text(data[0] if data else '')}")
            if literal is not None:
                imap.literal = literal
                status, data = imap.uid("SEARCH", "CHARSET", "UTF-8", *terms)
            else:
                status, data = imap.uid("SEARCH", *terms)
            if status != "OK":
                raise ProviderError(self.name, f"search failed: {self._text(data[0] if data else '')}")
            uids = sorted((data[0] or b"").split(), key=int)
            if query.uid_after is not None:
                # "UID n:*" always matches the newest message, even when its UID is below n.
                uids = [uid for uid in uids if int(uid) > query.uid_after]
            # UIDs ascend with arrival, so the newest N are at the end.
            selected = uids[: query.max_results] if query.oldest_first else uids[-query.max_results:]
            if not selected:
                return []
            uid_set = b",".join(selected).decode()

            status, headers = imap.uid("FETCH", uid_set, "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER])")
            if status != "OK":
                raise ProviderError(self.name, f"fetch failed: {self._text(headers[0] if headers else '')}")
            messages = {uid: (flags, size, data) for uid, flags, size, data in _fetch_parts(headers)}

            full: dict[str, bytes] = {}
            wanted = [uid for uid, (_, size, _) in messages.items() if size is None or size <= MAX_FULL_FETCH_BYTES]
            if wanted:
                status, bodies = imap.uid("FETCH", ",".join(wanted), "(UID BODY.PEEK[])")
                if status == "OK":
                    full = {uid: data for uid, _, _, data in _fetch_parts(bodies)}

            if query.mark_as_read:
                imap.uid("STORE", uid_set, "+FLAGS", "(\\Seen)")

            results = []
            for uid in sorted(messages, key=int, reverse=True):  # newest first
                flags, size, header_bytes = messages[uid]
                raw = full.get(uid)
                results.append(_parse_message(raw or header_bytes, uid, flags, size, query, raw is not None))
            return results
        except (OSError, imaplib.IMAP4.abort) as exc:
            raise ProviderError(self.name, f"IMAP connection error: {self._text(exc)}", retryable=True) from exc
        except imaplib.IMAP4.error as exc:
            raise ProviderError(self.name, f"IMAP error: {self._text(exc)}") from exc
        finally:
            self._logout(imap)

    def _status_sync(self, folder: str) -> dict[str, Any]:
        imap = self._connect()
        try:
            status, data = imap.status(self._mailbox_name(folder), "(UIDVALIDITY UIDNEXT MESSAGES)")
            if status != "OK" or not data or not data[0]:
                raise ProviderError(self.name, f"cannot read folder '{folder}': {self._text(data[0] if data else '')}")
            raw = data[0].decode("utf-8", "replace") if isinstance(data[0], bytes) else str(data[0])
            values = {key.lower(): int(value) for key, value in re.findall(r"(UIDVALIDITY|UIDNEXT|MESSAGES) (\d+)", raw)}
            if "uidvalidity" not in values or "uidnext" not in values:
                raise ProviderError(self.name, f"the server's STATUS reply for '{folder}' has no UIDVALIDITY/UIDNEXT: {raw[:200]}")
            return {"uidvalidity": values["uidvalidity"], "uidnext": values["uidnext"], "messages": values.get("messages")}
        except (OSError, imaplib.IMAP4.abort) as exc:
            raise ProviderError(self.name, f"IMAP connection error: {self._text(exc)}", retryable=True) from exc
        except imaplib.IMAP4.error as exc:
            raise ProviderError(self.name, f"IMAP error: {self._text(exc)}") from exc
        finally:
            self._logout(imap)

    def _verify_sync(self) -> dict[str, Any]:
        imap = self._connect()
        try:
            status, data = imap.select("INBOX", readonly=True)
            count = int(data[0]) if status == "OK" and data and data[0] else None
        finally:
            self._logout(imap)
        return {
            "imap_server": f"{self.account.imap_host}:{self.account.imap_port}",
            "username": self.account.username,
            "inbox_messages": count,
        }

    # --- MailboxProvider -------------------------------------------------------------

    async def fetch_emails(self, query: MailboxQuery) -> list[dict[str, Any]]:
        try:
            _search_criteria(query)  # surface bad filters before connecting
        except ValueError as exc:
            raise ProviderError(self.name, str(exc)) from exc
        return await with_retries(lambda: asyncio.to_thread(self._fetch_sync, query), self._retry)

    async def mailbox_status(self, folder: str = "INBOX") -> dict[str, Any]:
        return await with_retries(lambda: asyncio.to_thread(self._status_sync, folder), self._retry)

    async def verify(self) -> dict[str, Any]:
        return await with_retries(lambda: asyncio.to_thread(self._verify_sync), self._retry)
