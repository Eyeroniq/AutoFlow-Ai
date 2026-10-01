"""Notifications: Telegram (Bot API) and Discord (channel webhooks).

Both split text that is over the service's limit into several messages, retry 429s after
the wait the service asks for (flowforge_engine.providers.retry), and are not interruptible:
a stop lets a send in progress finish, so it is never unknown whether a message went out.
"""

from __future__ import annotations

import base64
import html
import mimetypes
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from flowforge_engine.errors import ProviderError
from flowforge_engine.files import FileNotAvailable
from flowforge_engine.models import GraphNode, NodeContext, NodeResult
from flowforge_engine.nodes.documents import FILE_FIELD, FileRef, _open_file
from flowforge_engine.nodes.integrations import AttachmentConfig
from flowforge_engine.providers.discord_provider import MAX_CONTENT_CHARS, MAX_EMBED_DESCRIPTION, MAX_EMBED_TITLE
from flowforge_engine.providers.settings import DiscordProviderName, TelegramProviderName
from flowforge_engine.providers.telegram_provider import MAX_MESSAGE_CHARS
from flowforge_engine.registry import GuardedOutboundConfig, NodeDefinition, register_node
from flowforge_engine.textutil import split_message
from flowforge_engine.variables import contains_reference

class _AnyType(frozenset):
    """A "set of allowed types" that allows every type (for _open_file)."""

    def __contains__(self, item: object) -> bool:
        return True


# Telegram's limits for a document and its caption.
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
MAX_CAPTION_CHARS = 1024
# Any uploaded file type may be sent as a document.
_ANY_TYPE = _AnyType()
# Markdown chunks are converted to HTML after splitting; leave room for the tags.
_TELEGRAM_SOURCE_CHARS = 3500


def _auth_requirement(node: GraphNode, provider: str) -> list[tuple[str, str]]:
    auth = node.config.get("auth", provider)
    if isinstance(auth, str) and not contains_reference(auth) and auth == provider:
        return [(provider, "auth")]
    return []


# --- Markdown -> Telegram HTML ------------------------------------------------------------

_FENCE = re.compile(r"```[\w+-]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__", re.DOTALL)
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)


def markdown_to_telegram_html(text: str) -> str:
    """The Markdown an LLM writes (bold, italics, headings, bullets, links, code) as the
    HTML subset Telegram renders. Everything else is escaped, so the text can't inject tags."""
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = _FENCE.sub(lambda m: keep(f"<pre>{html.escape(m.group(1).rstrip())}</pre>"), text)
    text = _INLINE_CODE.sub(lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = _LINK.sub(
        lambda m: keep(f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'), text
    )
    text = html.escape(text, quote=False)
    text = _HEADING.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _BULLET.sub(lambda m: f"{m.group(1)}• ", text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)
    text = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def _parse_error(exc: ProviderError) -> bool:
    return exc.status_code == 400 and "parse entities" in str(exc).lower()


# --- Telegram ------------------------------------------------------------------------------


class TelegramConfig(GuardedOutboundConfig):
    auth: TelegramProviderName = Field(
        default="telegram",
        description="'telegram' = your connected bot (or the server's TELEGRAM_BOT_TOKEN); 'mock' = send nothing.",
    )
    chat_id: str | int | None = Field(
        default=None,
        description="A user, group, or channel id (or @channelname). Blank: the Telegram integration's default chat (TELEGRAM_CHAT_ID).",
    )
    text: str = Field(
        default="",
        description="The message (with a document: its caption, or a message before it if over 1024 characters). Usually a reference such as {{digest.response}}.",
    )
    format: Literal["markdown", "html", "text"] = Field(
        default="markdown",
        description="markdown: **bold**, _italics_, headings, lists and links are rendered (falls back to plain text if Telegram rejects it). html: Telegram's HTML subset. text: sent as is.",
    )
    disable_link_preview: bool = Field(default=True, description="Don't show a preview card for the first link.")
    silent: bool = Field(default=False, description="Deliver without a notification sound.")
    split_long: bool = Field(
        default=True, description=f"Send text over {MAX_MESSAGE_CHARS} characters as several messages (off: fail instead)."
    )
    document: AttachmentConfig | None = Field(
        default=None,
        description="Optional file to send, from its content: e.g. an ICS Calendar node's {{ics.attachment}}.",
    )
    document_file: FileRef | None = Field(
        default=None, description="Optional uploaded file to send (instead of document).", json_schema_extra=FILE_FIELD
    )

    @model_validator(mode="after")
    def _something_to_send(self) -> TelegramConfig:
        if self.document is not None and self.document_file not in (None, ""):
            raise ValueError("send either document or document_file, not both")
        return self


class TelegramResult(BaseModel):
    message_ids: list[int]
    document: str | None = None
    chat_id: str
    parts: int
    format_used: str
    sent_at: str
    mock: bool


@register_node("telegram")
class TelegramNode(NodeDefinition[TelegramConfig]):
    category = "integration"
    guard_fields = ('text', 'document')
    label = "Telegram"
    description = "Sends a message through your Telegram bot (Bot API), split when it's long."
    icon = "send"
    config_schema = TelegramConfig
    output_schema = TelegramResult
    interruptible = False

    def required_providers(self, node: GraphNode) -> list[tuple[str, str]]:
        return _auth_requirement(node, "telegram")

    async def execute(self, context: NodeContext, config: TelegramConfig) -> NodeResult:
        chat_id = str(config.chat_id).strip() if config.chat_id not in (None, "") else None
        chat_id = chat_id or context.services.telegram_default_chat()
        if config.auth == "mock":
            chat_id = chat_id or "mock-chat"
        if not chat_id:
            return NodeResult.fail(
                "No chat to send to: set chat_id on the node, or a default chat id on the Telegram integration "
                "(TELEGRAM_CHAT_ID). Message your bot, then read the id from https://api.telegram.org/bot<token>/getUpdates"
            )
        text = config.text.strip()
        has_document = config.document is not None or config.document_file not in (None, "")
        if not text and not has_document:
            return NodeResult.fail("The message is empty")
        if has_document:
            return await self._send_document(context, config, chat_id, text)
        limit = _TELEGRAM_SOURCE_CHARS if config.format == "markdown" else MAX_MESSAGE_CHARS
        chunks = split_message(text, limit)
        if len(chunks) > 1 and not config.split_long:
            return NodeResult.fail(f"The message is {len(text)} characters; Telegram allows {MAX_MESSAGE_CHARS} (turn on split_long)")
        try:
            bot = context.services.telegram(config.auth)
        except ProviderError as exc:
            return NodeResult.fail(str(exc))

        message_ids: list[int] = []
        formats: set[str] = set()
        for chunk in chunks:
            body, mode = chunk, None
            if config.format == "markdown":
                converted = markdown_to_telegram_html(chunk)
                if len(converted) <= MAX_MESSAGE_CHARS:
                    body, mode = converted, "HTML"
            elif config.format == "html":
                mode = "HTML"
            options = {"disable_web_page_preview": config.disable_link_preview, "disable_notification": config.silent}
            try:
                try:
                    message = await bot.send_message(chat_id, body, parse_mode=mode, **options)
                    formats.add("html" if mode else "text")
                except ProviderError as exc:
                    if mode is None or not _parse_error(exc):
                        raise
                    # Telegram couldn't parse the markup: send the words rather than nothing.
                    message = await bot.send_message(chat_id, chunk, parse_mode=None, **options)
                    formats.add("text")
            except ProviderError as exc:
                sent = f" ({len(message_ids)} of {len(chunks)} parts were sent)" if message_ids else ""
                return NodeResult.fail(f"{exc}{sent}", message_ids=message_ids, chat_id=chat_id)
            message_ids.append(int(message.get("message_id", 0)))
        return NodeResult.ok(
            message_ids=message_ids,
            chat_id=chat_id,
            parts=len(message_ids),
            format_used="+".join(sorted(formats)),
            sent_at=datetime.now(UTC).isoformat(),
            mock=bool(getattr(bot, "is_mock", False)),
        )

    async def _send_document(self, context: NodeContext, config: TelegramConfig, chat_id: str, text: str) -> NodeResult:
        """The file, with the text as its caption (or as a message first when it's too long)."""
        try:
            if config.document is not None:
                doc = config.document
                data = base64.b64decode(doc.content, validate=True) if doc.encoding == "base64" else doc.content.encode("utf-8")
                filename = doc.filename
                content_type = doc.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            else:
                stored = await _open_file(context, config.document_file, _ANY_TYPE)
                data, filename, content_type = stored.path.read_bytes(), stored.filename, stored.content_type
        except FileNotAvailable as exc:
            return NodeResult.fail(str(exc))
        except ValueError:
            return NodeResult.fail("document.content isn't valid base64 (set encoding to text for plain content)")
        if len(data) > MAX_DOCUMENT_BYTES:
            return NodeResult.fail(f"'{filename}' is {len(data) / 1048576:.1f} MB; Telegram bots can send up to 50 MB")
        try:
            bot = context.services.telegram(config.auth)
        except ProviderError as exc:
            return NodeResult.fail(str(exc))
        message_ids: list[int] = []
        caption, mode = (text or None), None
        if caption and config.format == "markdown":
            converted = markdown_to_telegram_html(caption)
            caption, mode = (converted, "HTML") if len(converted) <= MAX_CAPTION_CHARS else (caption, None)
        elif caption and config.format == "html":
            mode = "HTML"
        try:
            if caption and len(caption) > MAX_CAPTION_CHARS:
                message = await bot.send_message(chat_id, text, disable_notification=config.silent)
                message_ids.append(int(message.get("message_id", 0)))
                caption, mode = None, None
            message = await bot.send_document(
                chat_id, filename, data, content_type, caption=caption, parse_mode=mode, disable_notification=config.silent
            )
        except ProviderError as exc:
            return NodeResult.fail(str(exc), message_ids=message_ids, chat_id=chat_id)
        message_ids.append(int(message.get("message_id", 0)))
        return NodeResult.ok(
            message_ids=message_ids, chat_id=chat_id, parts=len(message_ids), format_used="document",
            sent_at=datetime.now(UTC).isoformat(), mock=bool(getattr(bot, "is_mock", False)), document=filename,
        )


# --- Discord ---------------------------------------------------------------------------------


def _color(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = value.strip().lower()
    if text.isdigit() and not text.startswith("#"):
        return int(text)  # Discord's own decimal form
    digits = text.removeprefix("#").removeprefix("0x")
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    if len(digits) != 6 or any(c not in "0123456789abcdef" for c in digits):
        raise ValueError(f"embed_color '{value}' is not a hex color like #5865F2")
    return int(digits, 16)


class DiscordConfig(GuardedOutboundConfig):
    auth: DiscordProviderName = Field(
        default="discord",
        description="'discord' = your connected webhook (or DISCORD_WEBHOOK_URL); 'mock' = send nothing.",
    )
    webhook_url: str | None = Field(
        default=None,
        description="Blank: the Discord integration's webhook. A URL here is saved in the workflow and visible in run history.",
    )
    content: str = Field(default="", description=f"The message text (Discord Markdown). Over {MAX_CONTENT_CHARS} characters it's split.")
    username: str | None = Field(default=None, max_length=80, description="Overrides the webhook's name for this message.")
    avatar_url: str | None = None
    embed_title: str | None = Field(default=None, description="Optional embed under the message.")
    embed_description: str | None = None
    embed_url: str | None = None
    embed_color: str | int | None = Field(default=None, description="Hex, e.g. #5865F2.")
    embed_footer: str | None = None
    thread_id: str | None = Field(default=None, description="Post into this thread of the webhook's channel.")
    allow_mentions: bool = Field(default=False, description="Off: @everyone, @here, and user/role mentions don't ping anyone.")
    split_long: bool = Field(default=True, description="Send long content as several messages (off: fail instead).")


class DiscordResult(BaseModel):
    message_ids: list[str]
    channel_id: str | None
    parts: int
    sent_at: str
    mock: bool


@register_node("discord_webhook")
class DiscordWebhookNode(NodeDefinition[DiscordConfig]):
    category = "integration"
    guard_fields = ('content', 'embed_title', 'embed_description', 'embed_footer')
    label = "Discord Webhook"
    description = "Posts a message (and an optional embed) to a Discord channel through a webhook."
    icon = "message-square"
    config_schema = DiscordConfig
    output_schema = DiscordResult
    interruptible = False

    def required_providers(self, node: GraphNode) -> list[tuple[str, str]]:
        url = node.config.get("webhook_url")
        if isinstance(url, str) and url.strip():
            return []
        return _auth_requirement(node, "discord")

    async def execute(self, context: NodeContext, config: DiscordConfig) -> NodeResult:
        try:
            color = _color(config.embed_color)
        except ValueError as exc:
            return NodeResult.fail(str(exc))
        embed: dict[str, Any] = {}
        if config.embed_title:
            embed["title"] = config.embed_title[:MAX_EMBED_TITLE]
        if config.embed_description:
            embed["description"] = config.embed_description[:MAX_EMBED_DESCRIPTION]
        if config.embed_url:
            embed["url"] = config.embed_url
        if color is not None:
            embed["color"] = color
        if config.embed_footer:
            embed["footer"] = {"text": config.embed_footer[:2048]}
        chunks = split_message(config.content, MAX_CONTENT_CHARS)
        if not chunks and not embed:
            return NodeResult.fail("Nothing to send: set content or an embed")
        if len(chunks) > 1 and not config.split_long:
            return NodeResult.fail(f"The content is {len(config.content)} characters; Discord allows {MAX_CONTENT_CHARS} (turn on split_long)")
        try:
            webhook = context.services.discord(config.auth, webhook_url=(config.webhook_url or "").strip() or None)
        except ValueError as exc:
            return NodeResult.fail(f"webhook_url: {exc}")
        except ProviderError as exc:
            return NodeResult.fail(str(exc))

        base: dict[str, Any] = {"allowed_mentions": {"parse": ["users", "roles", "everyone"] if config.allow_mentions else []}}
        if config.username:
            base["username"] = config.username
        if config.avatar_url:
            base["avatar_url"] = config.avatar_url
        payloads = [{**base, "content": chunk} for chunk in chunks] or [dict(base)]
        if embed:
            payloads[-1]["embeds"] = [embed]
        message_ids: list[str] = []
        channel_id = None
        for payload in payloads:
            try:
                message = await webhook.execute(payload, thread_id=config.thread_id or None)
            except ProviderError as exc:
                sent = f" ({len(message_ids)} of {len(payloads)} parts were sent)" if message_ids else ""
                return NodeResult.fail(f"{exc}{sent}", message_ids=message_ids)
            message_ids.append(str(message.get("id", "")))
            channel_id = message.get("channel_id") or channel_id
        return NodeResult.ok(
            message_ids=message_ids,
            channel_id=str(channel_id) if channel_id is not None else None,
            parts=len(message_ids),
            sent_at=datetime.now(UTC).isoformat(),
            mock=bool(getattr(webhook, "is_mock", False)),
        )
