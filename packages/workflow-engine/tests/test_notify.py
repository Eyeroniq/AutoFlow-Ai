"""Telegram and Discord: the exact payloads sent, splitting long messages, Markdown to
Telegram HTML, 429 retry_after handling, error messages (with secrets redacted), and the
connection checks. The HTTP APIs are replaced by an in-process transport."""

import json

import httpx
import pytest

from flowforge_engine import ExecutionServices, IssueCode, NodeStatus, ProviderSettings, WorkflowGraph, execute_node, validate_workflow
from flowforge_engine.nodes.notify import markdown_to_telegram_html
from flowforge_engine.providers import RetryPolicy
from flowforge_engine.providers.discord_provider import DiscordWebhookProvider, parse_webhook_url
from flowforge_engine.providers.telegram_provider import TelegramProvider
from flowforge_engine.testing import make_context, node

TOKEN = "7123456789:AAFakeTokenForTests_abcdefghijklmno"
WEBHOOK = "https://discord.com/api/webhooks/112233445566778899/FakeWebhookToken-abcdefghijklmnopqrstuvwxyz"


class Recorder:
    """Answers with the queued responses in order (then `default`) and records requests."""

    def __init__(self, *responses, default=None):
        self.responses = list(responses)
        self.default = default
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0) if self.responses else self.default(request)
        if isinstance(response, Exception):
            raise response
        return response

    def body(self, index: int) -> dict:
        return json.loads(self.requests[index].content)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def ok_message(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 100, "chat": {"id": 42}}})


def telegram(recorder, *, max_retries=3):
    return TelegramProvider(
        TOKEN, transport=httpx.MockTransport(recorder.handler), sleep=recorder.sleep,
        retry=RetryPolicy(max_retries=max_retries, base_delay=0.01, max_delay=30),
    )


def telegram_services(recorder, chat_id=None, **kwargs):
    return ExecutionServices(
        provider_settings=ProviderSettings(telegram={"bot_token": TOKEN, "chat_id": chat_id}),
        messaging_providers={"telegram": telegram(recorder, **kwargs)},
    )


async def send_telegram(services, **config):
    return await execute_node(node("tg", "telegram", **config), make_context(services=services))


# --- Telegram ------------------------------------------------------------------------------


async def test_telegram_payload_markdown_rendered_as_html():
    recorder = Recorder(default=ok_message)
    result = await send_telegram(telegram_services(recorder), chat_id=42, text="**Digest** for _today_\n- one\n- two")
    assert result.status is NodeStatus.SUCCESS, result.error
    request = recorder.requests[0]
    assert request.method == "POST" and request.url.path == f"/bot{TOKEN}/sendMessage"
    assert recorder.body(0) == {
        "chat_id": "42",
        "text": "<b>Digest</b> for <i>today</i>\n• one\n• two",
        "parse_mode": "HTML",
        "disable_notification": False,
        "link_preview_options": {"is_disabled": True},
    }
    assert result.output == {
        **result.output, "message_ids": [100], "chat_id": "42", "parts": 1, "format_used": "html", "mock": False,
    }


def test_markdown_conversion_escapes_everything_else():
    source = "# Top\nUse `a<b>` & [docs](https://x.test/?a=1&b=2)\n```\nif x < 1: pass\n```\nsnake_case_name *stays*"
    html = markdown_to_telegram_html(source)
    assert html == (
        "<b>Top</b>\nUse <code>a&lt;b&gt;</code> &amp; <a href=\"https://x.test/?a=1&amp;b=2\">docs</a>\n"
        "<pre>if x &lt; 1: pass</pre>\nsnake_case_name <i>stays</i>"
    )
    assert markdown_to_telegram_html("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"


async def test_telegram_uses_the_default_chat_and_needs_one():
    recorder = Recorder(default=ok_message)
    result = await send_telegram(telegram_services(recorder, chat_id="777"), text="hi", format="text")
    assert result.status is NodeStatus.SUCCESS and recorder.body(0)["chat_id"] == "777"
    assert "parse_mode" not in recorder.body(0)

    none = await send_telegram(telegram_services(Recorder(default=ok_message)), text="hi")
    assert none.status is NodeStatus.FAILED and "No chat to send to" in none.error


async def test_telegram_429_waits_retry_after_then_succeeds():
    limited = httpx.Response(
        429, json={"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 3",
                   "parameters": {"retry_after": 3}},
    )
    recorder = Recorder(limited, default=ok_message)
    result = await send_telegram(telegram_services(recorder), chat_id=1, text="hello", format="text")
    assert result.status is NodeStatus.SUCCESS, result.error
    assert len(recorder.requests) == 2
    assert len(recorder.sleeps) == 1 and 3 <= recorder.sleeps[0] <= 3.25  # retry_after + a little jitter


async def test_telegram_gives_up_when_asked_to_wait_too_long():
    limited = httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests",
                                        "parameters": {"retry_after": 120}})
    recorder = Recorder(limited, default=ok_message)
    result = await send_telegram(telegram_services(recorder), chat_id=1, text="hello", format="text")
    assert result.status is NodeStatus.FAILED
    assert "rate limited (HTTP 429)" in result.error and "asked to wait 120s" in result.error
    assert recorder.sleeps == [] and len(recorder.requests) == 1


async def test_telegram_falls_back_to_plain_text_when_markup_is_rejected():
    rejected = httpx.Response(400, json={"ok": False, "error_code": 400,
                                         "description": "Bad Request: can't parse entities: unclosed tag"})
    recorder = Recorder(rejected, default=ok_message)
    result = await send_telegram(telegram_services(recorder), chat_id=5, text="**bold** text")
    assert result.status is NodeStatus.SUCCESS and result.output["format_used"] == "text"
    assert recorder.body(0)["parse_mode"] == "HTML"
    assert recorder.body(1)["text"] == "**bold** text" and "parse_mode" not in recorder.body(1)


async def test_telegram_splits_long_messages():
    recorder = Recorder(default=ok_message)
    paragraphs = "\n\n".join(f"Paragraph {i}: " + "word " * 150 for i in range(12))  # ~9,000 characters
    result = await send_telegram(telegram_services(recorder), chat_id=1, text=paragraphs, format="text")
    assert result.status is NodeStatus.SUCCESS and result.output["parts"] == len(recorder.requests) >= 3
    texts = [recorder.body(i)["text"] for i in range(len(recorder.requests))]
    assert all(len(t) <= 4096 for t in texts)
    assert " ".join(" ".join(texts).split()) == " ".join(paragraphs.split())  # nothing lost

    refused = await send_telegram(telegram_services(Recorder(default=ok_message)), chat_id=1, text=paragraphs,
                                  format="text", split_long=False)
    assert refused.status is NodeStatus.FAILED and "turn on split_long" in refused.error


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}), "bot token was rejected"),
        (httpx.Response(400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}),
         "send it a message"),
        (httpx.Response(403, json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}),
         "press Start"),
    ],
)
async def test_telegram_errors_explain_what_to_do(response, expected):
    result = await send_telegram(telegram_services(Recorder(response)), chat_id=1, text="x", format="text")
    assert result.status is NodeStatus.FAILED and expected in result.error


async def test_telegram_token_never_appears_in_errors():
    recorder = Recorder(httpx.ConnectError(f"connection refused for https://api.telegram.org/bot{TOKEN}/sendMessage"))
    result = await send_telegram(telegram_services(recorder, max_retries=0), chat_id=1, text="x", format="text")
    assert result.status is NodeStatus.FAILED and "network error" in result.error
    assert TOKEN not in result.error and "[REDACTED]" in result.error


async def test_telegram_verify_checks_the_token_and_the_chat():
    recorder = Recorder(
        httpx.Response(200, json={"ok": True, "result": {"id": 7, "username": "flowforge_bot", "first_name": "FlowForge"}}),
        httpx.Response(200, json={"ok": True, "result": {"id": 42, "type": "private", "first_name": "Sam"}}),
    )
    details = await telegram(recorder).verify("42")
    assert details == {"bot": "@flowforge_bot", "bot_id": 7, "bot_name": "FlowForge",
                       "chat": {"id": 42, "type": "private", "title": "Sam"}}
    assert [r.url.path.rsplit("/", 1)[1] for r in recorder.requests] == ["getMe", "getChat"]


def test_telegram_needs_a_bot_token_to_validate():
    graph = WorkflowGraph(nodes=[node("tg", "telegram", text="hi", chat_id=1)])
    issues = validate_workflow(graph, services=ExecutionServices(provider_settings=ProviderSettings()))
    assert [i.code for i in issues] == [IssueCode.AUTH_MISSING] and "@BotFather" in issues[0].message
    mocked = WorkflowGraph(nodes=[node("tg", "telegram", text="hi", auth="mock")])
    assert validate_workflow(mocked, services=ExecutionServices(provider_settings=ProviderSettings())) == []


# --- Discord -------------------------------------------------------------------------------


def discord_created(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"id": f"m{len(request.content)}", "channel_id": "555", **json.loads(request.content)})


def discord_services(recorder, *, max_retries=3):
    provider = DiscordWebhookProvider(
        WEBHOOK, transport=httpx.MockTransport(recorder.handler), sleep=recorder.sleep,
        retry=RetryPolicy(max_retries=max_retries, base_delay=0.01, max_delay=30),
    )
    return ExecutionServices(provider_settings=ProviderSettings(), messaging_providers={"discord": provider})


async def post_discord(services, **config):
    return await execute_node(node("dc", "discord_webhook", **config), make_context(services=services))


async def test_discord_payload_with_embed_and_no_pings():
    recorder = Recorder(default=discord_created)
    result = await post_discord(
        discord_services(recorder), content="New jobs: @everyone look", username="FlowForge",
        embed_title="3 matches", embed_description="Top: Python dev", embed_url="https://jobs.example.com",
        embed_color="#5865F2", embed_footer="Job Alert",
    )
    assert result.status is NodeStatus.SUCCESS, result.error
    request = recorder.requests[0]
    assert request.method == "POST" and request.url.params["wait"] == "true"
    assert recorder.body(0) == {
        "allowed_mentions": {"parse": []},
        "username": "FlowForge",
        "content": "New jobs: @everyone look",
        "embeds": [{
            "title": "3 matches", "description": "Top: Python dev", "url": "https://jobs.example.com",
            "color": 0x5865F2, "footer": {"text": "Job Alert"},
        }],
    }
    assert result.output["channel_id"] == "555" and result.output["parts"] == 1


async def test_discord_429_honours_retry_after():
    limited = httpx.Response(429, json={"message": "You are being rate limited.", "retry_after": 1.5, "global": False})
    recorder = Recorder(limited, default=discord_created)
    result = await post_discord(discord_services(recorder), content="hello")
    assert result.status is NodeStatus.SUCCESS, result.error
    assert len(recorder.requests) == 2 and 1.5 <= recorder.sleeps[0] <= 1.75


async def test_discord_retry_after_header_is_used_without_a_body():
    limited = httpx.Response(429, headers={"retry-after": "2"})
    recorder = Recorder(limited, default=discord_created)
    result = await post_discord(discord_services(recorder), content="hello")
    assert result.status is NodeStatus.SUCCESS and 2 <= recorder.sleeps[0] <= 2.25


async def test_discord_splits_content_and_puts_the_embed_last():
    recorder = Recorder(default=discord_created)
    content = "\n".join(f"Line {i} " + "x" * 90 for i in range(45))  # ~4,500 characters
    result = await post_discord(discord_services(recorder), content=content, embed_title="Summary")
    assert result.status is NodeStatus.SUCCESS and result.output["parts"] == 3
    bodies = [recorder.body(i) for i in range(3)]
    assert all(len(b["content"]) <= 2000 for b in bodies)
    assert [("embeds" in b) for b in bodies] == [False, False, True]


async def test_discord_rejects_non_webhook_urls_and_empty_messages():
    services = ExecutionServices(provider_settings=ProviderSettings())
    bad = await post_discord(services, webhook_url="https://evil.example.com/api/webhooks/1/x", content="hi")
    assert bad.status is NodeStatus.FAILED and bad.error.startswith("webhook_url: not a Discord webhook URL")
    empty = await post_discord(discord_services(Recorder(default=discord_created)), content="  ")
    assert empty.status is NodeStatus.FAILED and "Nothing to send" in empty.error
    assert parse_webhook_url(WEBHOOK)[0] == "112233445566778899"
    assert parse_webhook_url("https://canary.discordapp.com/api/v10/webhooks/1/abc-_d")[1] == "abc-_d"


async def test_discord_deleted_webhook_error_hides_the_token():
    gone = httpx.Response(404, json={"message": "Unknown Webhook", "code": 10015})
    result = await post_discord(discord_services(Recorder(gone)), content="hi")
    assert result.status is NodeStatus.FAILED and "Unknown Webhook" in result.error and "Create a new one" in result.error
    assert "FakeWebhookToken" not in result.error


async def test_discord_verify_gets_the_webhook_without_posting():
    recorder = Recorder(httpx.Response(200, json={"name": "Alerts", "channel_id": "555", "guild_id": "9"}))
    provider = discord_services(recorder).discord()
    assert await provider.verify() == {"webhook": "Alerts", "channel_id": "555", "guild_id": "9"}
    assert recorder.requests[0].method == "GET"


def test_discord_node_needs_a_webhook_from_config_or_credentials():
    bare = ExecutionServices(provider_settings=ProviderSettings())
    missing = validate_workflow(WorkflowGraph(nodes=[node("dc", "discord_webhook", content="hi")]), services=bare)
    assert [i.code for i in missing] == [IssueCode.AUTH_MISSING]
    inline = WorkflowGraph(nodes=[node("dc", "discord_webhook", content="hi", webhook_url=WEBHOOK)])
    assert validate_workflow(inline, services=bare) == []
