"""Sending Telegram messages through the Bot API (https://core.telegram.org/bots/api).

Every call is a POST to https://api.telegram.org/bot<token>/<method> with a JSON body; the
answer is {"ok": true, "result": ...} or {"ok": false, "error_code": ..., "description": ...}.
A 429 carries `parameters.retry_after` (seconds), which the retry helper honours. The token
is part of the URL, so it is redacted from every error message.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from flowforge_engine.errors import ProviderError
from flowforge_engine.providers.retry import RetryPolicy, redact, with_retries

API_BASE = "https://api.telegram.org"
# Telegram rejects longer message texts.
MAX_MESSAGE_CHARS = 4096


def _hint(description: str) -> str:
    text = description.lower()
    if "chat not found" in text:
        return (
            " The bot can't see that chat: open your bot in Telegram and send it a message (or add it to the "
            "group), then check the chat id."
        )
    if "bot was blocked" in text or "can't initiate conversation" in text:
        return " Open the bot in Telegram and press Start (or unblock it)."
    if "not enough rights" in text:
        return " Give the bot permission to post in that chat."
    return ""


class TelegramProvider:
    is_mock = False

    def __init__(
        self,
        bot_token: str,
        *,
        name: str = "telegram",
        retry: RetryPolicy | None = None,
        timeout: float = 30,
        api_base: str = API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if not bot_token.strip():
            raise ValueError("TelegramProvider needs a bot token")
        self.name = name
        self._token = bot_token.strip()
        self._retry = retry or RetryPolicy()
        self._timeout = timeout
        self._api_base = api_base.rstrip("/")
        self._transport = transport
        self._sleep = sleep

    def _clean(self, text: str) -> str:
        return redact(text, self._token)

    def _error(self, method: str, status: int, description: str, retry_after: float | None) -> ProviderError:
        description = self._clean(description)
        if status == 401:
            return ProviderError(
                self.name,
                f"{method}: the bot token was rejected (HTTP 401: {description}). Check TELEGRAM_BOT_TOKEN "
                "or the telegram credential under Integrations",
                status_code=status,
            )
        if status == 429:
            return ProviderError(
                self.name, f"{method}: rate limited (HTTP 429): {description}",
                status_code=status, retryable=True, retry_after=retry_after,
            )
        if status >= 500:
            return ProviderError(
                self.name, f"{method}: server error (HTTP {status}): {description}",
                status_code=status, retryable=True, retry_after=retry_after,
            )
        return ProviderError(self.name, f"{method}: {description} (HTTP {status}).{_hint(description)}", status_code=status)

    async def _call_once(
        self, method: str, payload: dict[str, Any], files: dict[str, tuple[str, bytes, str]] | None = None
    ) -> Any:
        url = f"{self._api_base}/bot{self._token}/{method}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                if files:
                    # multipart/form-data: plain fields as strings, the file as an upload.
                    form = {k: v if isinstance(v, str) else json.dumps(v) for k, v in payload.items()}
                    response = await client.post(url, data=form, files=files)
                else:
                    response = await client.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, f"{method}: no answer within {self._timeout:g}s", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.name, f"{method}: network error: {type(exc).__name__}: {self._clean(str(exc))}", retryable=True
            ) from exc
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if response.status_code == 200 and data.get("ok") is True:
            return data.get("result")
        status = int(data.get("error_code") or response.status_code)
        description = str(data.get("description") or response.text[:300] or "no description")
        parameters = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
        retry_after = parameters.get("retry_after")
        if retry_after is None and status == 429:
            try:
                retry_after = float(response.headers.get("retry-after", ""))
            except ValueError:
                retry_after = None
        raise self._error(method, status, description, float(retry_after) if retry_after is not None else None)

    async def call(
        self, method: str, payload: dict[str, Any] | None = None, files: dict[str, tuple[str, bytes, str]] | None = None
    ) -> Any:
        """One Bot API method, retried on 429 (after `retry_after`), 5xx, and network errors."""
        return await with_retries(lambda: self._call_once(method, payload or {}, files), self._retry, sleep=self._sleep)

    async def send_document(
        self,
        chat_id: str,
        filename: str,
        data: bytes,
        content_type: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        """sendDocument (a file of up to 50 MB, with an optional caption of up to 1024 characters)."""
        payload: dict[str, Any] = {"chat_id": str(chat_id), "disable_notification": disable_notification}
        if caption:
            payload["caption"] = caption
            if parse_mode:
                payload["parse_mode"] = parse_mode
        return await self.call("sendDocument", payload, files={"document": (filename, data, content_type)})

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = False,
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        """sendMessage; returns Telegram's Message object."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
            "link_preview_options": {"is_disabled": disable_web_page_preview},
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return await self.call("sendMessage", payload)

    async def verify(self, chat_id: str | None = None) -> dict[str, Any]:
        """getMe proves the token works; with a chat id, getChat proves the bot can reach it.
        Nothing is sent."""
        me = await self.call("getMe")
        details: dict[str, Any] = {"bot": f"@{me.get('username')}", "bot_id": me.get("id"), "bot_name": me.get("first_name")}
        if chat_id:
            chat = await self.call("getChat", {"chat_id": chat_id})
            details["chat"] = {
                "id": chat.get("id"),
                "type": chat.get("type"),
                "title": chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or None,
            }
        return details
