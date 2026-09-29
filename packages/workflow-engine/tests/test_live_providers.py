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
        one = len(missing) == 1
        pytest.skip(f"{' and '.join(missing)} {'is' if one else 'are'} blank; set {'it' if one else 'them'} in .env to run this live test")


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


async def run_llm_node(node_type, settings=None, **config):
    """One LLM node of `node_type` in a real graph; returns its output."""
    graph = WorkflowGraph(
        nodes=[node("llm", node_type, user_prompt=PROMPT, max_tokens=512, **config), node("out", "output", value="{{llm.response}}")],
        edges=chain("llm", "out"),
    )
    start = time.perf_counter()
    result = await execute_graph(graph, make_context(services=ExecutionServices(provider_settings=settings or live_settings())))
    assert result.status is RunStatus.SUCCESS, result.error
    output = result.result_for("llm").output
    report(f"{node_type} node", provider_used=output["provider_used"], model=output["model"], mock=output["mock"],
           latency_ms=round((time.perf_counter() - start) * 1000), response=repr(output["response"][:160]))
    assert output["mock"] is False and output["response"].strip()
    return output


class TestNewLLMProviders:
    async def test_mistral_node(self):
        require("MISTRAL_API_KEY")
        await run_llm_node("mistral")

    async def test_cerebras_node(self):
        require("CEREBRAS_API_KEY")
        await run_llm_node("cerebras")

    async def test_custom_node(self):
        require("CUSTOM_OPENAI_BASE_URL", "CUSTOM_OPENAI_MODEL")
        await run_llm_node("custom_llm")

    async def test_custom_node_against_groqs_openai_compatible_endpoint(self):
        """The Custom provider end to end with a real OpenAI-compatible server: Groq's, with
        GROQ_API_KEY (nothing is stored; the settings exist only in this test)."""
        require("GROQ_API_KEY")
        env = live_env()
        settings = live_settings().model_copy(update={"custom": live_settings().custom.model_validate({
            "api_key": env["GROQ_API_KEY"], "base_url": "https://api.groq.com/openai/v1", "model": "openai/gpt-oss-20b"})})
        output = await run_llm_node("custom_llm", settings)
        assert output["provider_used"] == "custom" and output["model"] == "openai/gpt-oss-20b"

    async def test_custom_endpoint_on_a_private_address_is_refused(self):
        settings = live_settings().model_copy(update={"custom": live_settings().custom.model_validate({
            "base_url": "http://169.254.169.254/v1", "model": "m"})})
        with pytest.raises(Exception, match="Blocked"):
            get_llm_provider("custom", settings)


class TestAudioAndSearch:
    async def test_groq_whisper_on_the_sample_meeting(self, tmp_path):
        require("GROQ_API_KEY")
        from pathlib import Path

        from flowforge_engine.providers import get_transcriber

        sample = Path(__file__).resolve().parents[3] / "samples" / "team-meeting.mp3"
        if not sample.exists():
            sample = Path("/samples/team-meeting.mp3")
        settings = live_settings()
        start = time.perf_counter()
        out = await get_transcriber("groq", settings).transcribe(sample, model=settings.speech.groq_model, prompt="FlowForge, Groq")
        report("groq whisper", latency_ms=round((time.perf_counter() - start) * 1000), language=out["language"],
               segments=len(out["segments"]), text=repr(out["text"][:120]))
        assert out["language"] == "en" and "October" in out["text"]

    async def test_duckduckgo_search(self):
        from flowforge_engine.providers import get_search_provider

        results = await get_search_provider("duckduckgo", live_settings()).search("QUIC transport protocol RFC 9000", max_results=3)
        report("duckduckgo", count=len(results), urls=[r["url"] for r in results])
        assert results and all(r["url"].startswith("http") for r in results)

    async def test_tavily_search_and_usage(self):
        require("TAVILY_API_KEY")
        from flowforge_engine.providers import get_search_provider

        tavily = get_search_provider("tavily", live_settings())
        results = await tavily.search("QUIC transport protocol RFC 9000", max_results=3)
        report("tavily", count=len(results), urls=[r["url"] for r in results], usage=await tavily.verify())
        assert results


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

