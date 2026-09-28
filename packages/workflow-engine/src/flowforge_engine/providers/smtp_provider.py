"""Sending email over SMTP (Gmail with an App Password by default).

smtplib is blocking, so every network call runs in a worker thread via asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import mimetypes
import smtplib
import ssl
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from typing import Any

from flowforge_engine.errors import ProviderError
from flowforge_engine.providers.base import MAX_ATTACHMENT_BYTES, OutgoingEmail
from flowforge_engine.providers.retry import RetryPolicy, redact, with_retries
from flowforge_engine.providers.settings import EmailAccount

APP_PASSWORD_HINT = (
    "Gmail needs a 16-character Google App Password (not your normal password) with 2-Step "
    "Verification enabled; see the README section 'Gmail: create an App Password'"
)


def is_gmail_host(host: str) -> bool:
    return host.endswith(("gmail.com", "googlemail.com"))


def login_password(account: EmailAccount) -> str:
    password = account.password.get_secret_value() if account.password else ""
    # Google displays App Passwords as "abcd efgh ijkl mnop"; the spaces aren't part of it.
    return "".join(password.split()) if is_gmail_host(account.smtp_host) else password


def build_message(email: OutgoingEmail, *, sender: str, from_name: str | None = None) -> EmailMessage:
    """A MIME message: plain text, optional HTML alternative, attachments. Bcc stays off the headers."""
    message = EmailMessage()
    message["From"] = formataddr((from_name, sender)) if from_name else sender
    message["To"] = ", ".join(email.to)
    if email.cc:
        message["Cc"] = ", ".join(email.cc)
    message["Subject"] = email.subject
    message["Date"] = format_datetime(datetime.now(UTC))
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    message.set_content(email.body)
    if email.html_body:
        message.add_alternative(email.html_body, subtype="html")

    total = 0
    for attachment in email.attachments:
        data = attachment.data()
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"attachments exceed {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB in total")
        content_type = attachment.content_type or mimetypes.guess_type(attachment.filename)[0]
        if not content_type:
            content_type = "text/plain" if attachment.encoding == "text" else "application/octet-stream"
        maintype, _, subtype = content_type.partition("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=attachment.filename)
    return message


class SMTPEmailProvider:
    is_mock = False

    def __init__(
        self,
        account: EmailAccount,
        *,
        name: str = "gmail",
        retry: RetryPolicy | None = None,
        smtp_factory: Callable[..., smtplib.SMTP] | None = None,
    ):
        if not account.configured:
            raise ValueError("SMTPEmailProvider needs a username and password")
        self.name = name
        self.account = account
        self._retry = retry or RetryPolicy()
        self._factory = smtp_factory

    # --- connection (blocking; runs in a worker thread) ------------------------------

    def _connect(self) -> smtplib.SMTP:
        account = self.account
        context = ssl.create_default_context()
        try:
            if account.resolved_smtp_security == "ssl":
                factory = self._factory or smtplib.SMTP_SSL
                kwargs: dict[str, Any] = {"timeout": account.timeout_seconds}
                if factory is smtplib.SMTP_SSL:
                    kwargs["context"] = context
                smtp = factory(account.smtp_host, account.smtp_port, **kwargs)
            else:
                factory = self._factory or smtplib.SMTP
                smtp = factory(account.smtp_host, account.smtp_port, timeout=account.timeout_seconds)
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
        except smtplib.SMTPResponseException as exc:
            raise self._response_error(exc, "connecting") from exc
        except smtplib.SMTPNotSupportedError as exc:
            raise ProviderError(
                self.name, f"{account.smtp_host}:{account.smtp_port} doesn't support {exc}; check SMTP_SECURITY/port"
            ) from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise ProviderError(
                self.name,
                f"cannot connect to {account.smtp_host}:{account.smtp_port}: {type(exc).__name__}: {exc}",
                retryable=True,
            ) from exc

        try:
            smtp.login(account.username or "", login_password(account))
        except smtplib.SMTPAuthenticationError as exc:
            smtp.close()
            raise ProviderError(
                self.name,
                f"login rejected for {account.username} ({exc.smtp_code}: {self._text(exc.smtp_error)}). "
                + (APP_PASSWORD_HINT if is_gmail_host(account.smtp_host) else "Check the username and password"),
                status_code=exc.smtp_code,
            ) from exc
        except smtplib.SMTPResponseException as exc:
            smtp.close()
            raise self._response_error(exc, "logging in") from exc
        except (OSError, smtplib.SMTPException) as exc:
            smtp.close()
            raise ProviderError(self.name, f"login failed: {type(exc).__name__}: {exc}", retryable=True) from exc
        return smtp

    def _text(self, raw: bytes | str) -> str:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        return redact(" ".join(text.split()), login_password(self.account))

    def _response_error(self, exc: smtplib.SMTPResponseException, phase: str) -> ProviderError:
        # 4xx replies are temporary; the message was not accepted, so a retry can't duplicate it.
        return ProviderError(
            self.name,
            f"SMTP error while {phase} ({exc.smtp_code}: {self._text(exc.smtp_error)})",
            status_code=exc.smtp_code,
            retryable=400 <= exc.smtp_code < 500,
        )

    def _send_sync(self, message: EmailMessage, recipients: list[str]) -> dict[str, Any]:
        smtp = self._connect()
        try:
            refused = smtp.send_message(message, from_addr=self.account.username, to_addrs=recipients)
        except smtplib.SMTPRecipientsRefused as exc:
            details = ", ".join(f"{addr} ({code})" for addr, (code, _) in exc.recipients.items())
            raise ProviderError(self.name, f"all recipients were refused: {details}") from exc
        except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
            raise self._response_error(exc, "sending") from exc
        except (smtplib.SMTPServerDisconnected, OSError) as exc:
            # The server may have accepted the message before the connection dropped, so
            # this is not retried (a retry could deliver it twice).
            raise ProviderError(
                self.name, f"connection lost while sending; the email may not have been delivered ({exc})"
            ) from exc
        finally:
            try:
                smtp.quit()
            except (smtplib.SMTPException, OSError):
                smtp.close()
        return {addr: f"{code} {self._text(reply)}" for addr, (code, reply) in refused.items()}

    def _verify_sync(self) -> dict[str, Any]:
        smtp = self._connect()
        try:
            smtp.noop()
        finally:
            try:
                smtp.quit()
            except (smtplib.SMTPException, OSError):
                smtp.close()
        return {
            "smtp_server": f"{self.account.smtp_host}:{self.account.smtp_port}",
            "security": self.account.resolved_smtp_security,
            "username": self.account.username,
        }

    # --- EmailProvider ---------------------------------------------------------------

    async def send_email(self, email: OutgoingEmail) -> dict[str, Any]:
        sender = self.account.username or ""
        try:
            message = build_message(email, sender=sender, from_name=self.account.from_name)
        except ValueError as exc:
            raise ProviderError(self.name, str(exc)) from exc
        recipients = [*email.to, *email.cc, *email.bcc]

        refused = await with_retries(lambda: asyncio.to_thread(self._send_sync, message, recipients), self._retry)
        return {
            "message_id": message["Message-ID"],
            "status": "sent" if not refused else "partially_sent",
            "provider": self.name,
            "from": sender,
            "to": list(email.to),
            "cc": list(email.cc),
            "bcc": list(email.bcc),
            "subject": email.subject,
            "attachments": [a.filename for a in email.attachments],
            "refused": refused,
            "sent_at": datetime.now(UTC).isoformat(),
            "mock": False,
        }

    async def verify(self) -> dict[str, Any]:
        return await with_retries(lambda: asyncio.to_thread(self._verify_sync), self._retry)
