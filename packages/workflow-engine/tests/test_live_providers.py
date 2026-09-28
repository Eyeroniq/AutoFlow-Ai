"""Live provider checks against the real services. Run with:  pytest -m live -s

Each test skips (naming the missing variable) unless its key/service is available.
Keys come from the environment or the repo .env and are never printed.
"""

import time

import httpx
import pytest

from flowforge_engine import ExecutionServices, RunStatus, WorkflowGraph, execute_graph
from flowforge_engine.providers import MailboxQuery, get_email_provider, get_llm_provider, get_mailbox_provider
from flowforge_engine.testing import chain, live_env, live_settings, make_context, node

pytestmark = pytest.mark.live

PROMPT = "In one short sentence, what is a workflow automation tool?"


def require(*names):
    env = live_env()
    missing = [name for name in names if not env.get(name)]
    if missing:
        pytest.skip(f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} blank; set it in .env to run this live test")


def report(label, **fields):
    print(f"\n[live] {label}: " + " | ".join(f"{k}={v}" for k, v in fields.items()))


async def generate(provider_name):
    settings = live_settings()
    provider = get_llm_provider(provider_name, settings)
    model = settings.default_model(provider_name)
    start = time.perf_counter()
    text = await provider.generate("You are concise.", PROMPT, model, 0.3, 1024)
    report(provider_name, model=model, latency_ms=round((time.perf_counter() - start) * 1000), text=repr(text[:160]))
    assert text.strip()
    return text


class TestGemini:
    async def test_generate_stream_and_embed(self):
        require("GEMINI_API_KEY")
        await generate("gemini")

        settings = live_settings()
        provider = get_llm_provider("gemini", settings)
        pieces = [p async for p in provider.stream("", "Count from 1 to 5, separated by spaces.", settings.default_model("gemini"), 0, 256)]
        report("gemini stream", chunks=len(pieces), text=repr("".join(pieces)[:80]))
        assert "".join(pieces).strip()

        vector = await provider.embed("workflow automation")
        report("gemini embed", model=settings.embedding_model("gemini"), dimensions=len(vector))
        assert len(vector) > 100

        details = await provider.verify(settings.default_model("gemini"))
        report("gemini verify", **details)

    async def test_gemini_node_in_a_graph(self):
        require("GEMINI_API_KEY")
        graph = WorkflowGraph(
            nodes=[
                node("topic", "input", name="topic"),
                node("gemini", "gemini", user_prompt="Give a one-line definition of {{topic.value}}.", max_tokens=1024),
                node("out", "output", value="{{gemini.response}}"),
            ],
            edges=chain("topic", "gemini", "out"),
        )
        context = make_context(inputs={"topic": "a webhook"}, services=ExecutionServices(provider_settings=live_settings()))
        result = await execute_graph(graph, context)
        assert result.status is RunStatus.SUCCESS, result.error
        output = result.result_for("gemini").output
        report("gemini node", provider_used=output["provider_used"], model=output["model"], mock=output["mock"],
               response=repr(output["response"][:160]))
        assert output["provider_used"] == "gemini" and output["mock"] is False


class TestOpenAICompatible:
    async def test_groq(self):
        require("GROQ_API_KEY")
        await generate("groq")

    async def test_openrouter_free_model(self):
        require("OPENROUTER_API_KEY")
        await generate("openrouter")

    async def test_ollama(self):
        settings = live_settings()
        base_url = settings.base_url("ollama")
        try:
            models = httpx.get(f"{base_url}/models", timeout=3).json()
        except (httpx.HTTPError, ValueError):
            pytest.skip(f"Ollama is not reachable at {base_url} (install it and run `ollama serve`)")
        model = settings.default_model("ollama")
        pulled = {m["id"] for m in models.get("data", [])}
        if model not in pulled and f"{model}:latest" not in pulled:
            pytest.skip(f"Ollama is running but '{model}' isn't pulled; run `ollama pull {model}`")
        await generate("ollama")


async def test_fallback_chain_reports_the_provider_that_answered():
    """Primary 'ollama' (usually not running here) falls back to Gemini; provider_used says who answered."""
    require("GEMINI_API_KEY")
    graph = WorkflowGraph(
        nodes=[node("llm", "ollama", user_prompt="Reply with the single word: pong", fallback=["gemini"], max_tokens=512)],
        edges=[],
    )
    settings = live_settings().model_copy(update={"retry": live_settings().retry.model_copy(update={"max_retries": 1})})
    result = await execute_graph(graph, make_context(services=ExecutionServices(provider_settings=settings)))
    assert result.status is RunStatus.SUCCESS, result.error
    output = result.result_for("llm").output
    report("fallback chain", provider=output["provider"], provider_used=output["provider_used"],
           failed=[e["provider"] for e in output["fallback_errors"]], response=repr(output["response"][:60]))
    assert output["provider_used"] in {"ollama", "gemini"}


class TestGmailLive:
    async def test_smtp_and_imap_login(self):
        require("SMTP_USER", "SMTP_PASSWORD")
        settings = live_settings()
        smtp = await get_email_provider("gmail", settings).verify()
        report("smtp login", **smtp)
        imap = await get_mailbox_provider("gmail", settings).verify()
        report("imap login", **imap)

    async def test_read_recent_mail(self):
        require("SMTP_USER", "SMTP_PASSWORD")
        mailbox = get_mailbox_provider("gmail", live_settings())
        emails = await mailbox.fetch_emails(MailboxQuery(unread_only=False, max_results=3, include_body=False))
        report("imap read", count=len(emails), subjects=[e["subject"][:40] for e in emails])
        assert isinstance(emails, list)

