"""Web Search: DuckDuckGo's error mapping (rate limits, no results), Tavily's requests,
errors, and usage check, the node's fallback between them, empty results, and fetching
result pages behind the SSRF guard. Also the custom OpenAI-compatible provider's SSRF guard
and the Web Page node's fail_on_error."""

import json

import httpx
import pytest
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from flowforge_engine import ExecutionServices, IssueCode, NodeStatus, ProviderSettings, WorkflowGraph, execute_node, validate_workflow
from flowforge_engine.errors import ProviderError
from flowforge_engine.netguard import GuardedNetworkBackend
from flowforge_engine.providers import RetryPolicy, get_llm_provider
from flowforge_engine.providers.openai_compatible import OpenAICompatibleProvider
from flowforge_engine.providers.search import DuckDuckGoSearch, TavilySearch
from flowforge_engine.testing import make_context, node

TAVILY_KEY = "tvly-FakeKeyForTests-0123456789abcdef"


class FakeDDGS:
    """Stands in for ddgs.DDGS: returns rows or raises the given exception."""

    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.calls = rows or [], error, []

    def __call__(self, timeout):
        return self

    def text(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        if self.error:
            raise self.error
        return self.rows


ROWS = [
    {"title": "Asyncio docs", "href": "https://docs.python.org/3/library/asyncio.html", "body": "asyncio is a  library\nto write"},
    {"title": "", "href": "https://realpython.com/async-io-python/", "body": "A walkthrough"},
    {"title": "Broken", "href": "javascript:alert(1)", "body": "not a web page"},
]


async def test_duckduckgo_results_and_parameters():
    ddgs = FakeDDGS(ROWS)
    results = await DuckDuckGoSearch(ddgs_factory=ddgs).search("asyncio", max_results=5, region="us-en", time_range="week")
    assert results == [
        {"title": "Asyncio docs", "url": "https://docs.python.org/3/library/asyncio.html", "snippet": "asyncio is a library to write"},
        {"title": "https://realpython.com/async-io-python/", "url": "https://realpython.com/async-io-python/", "snippet": "A walkthrough"},
    ]
    assert ddgs.calls[0] == {"query": "asyncio", "region": "us-en", "safesearch": "moderate", "timelimit": "w",
                             "max_results": 5, "backend": "duckduckgo"}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RatelimitException("https://html.duckduckgo.com/html 202 Ratelimit"), "rate limiting this server"),
        (TimeoutException("timed out"), "didn't answer within"),
        (DDGSException("unexpected response"), "DuckDuckGo search failed"),
    ],
)
async def test_duckduckgo_errors_say_what_happened(error, expected):
    with pytest.raises(ProviderError, match=expected):
        await DuckDuckGoSearch(ddgs_factory=FakeDDGS(error=error)).search("x")


async def test_duckduckgo_no_results_is_an_empty_list():
    assert await DuckDuckGoSearch(ddgs_factory=FakeDDGS(error=DDGSException("No results found."))).search("zzqq") == []


class TavilyApi:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def handler(self, request):
        self.requests.append(request)
        return self.responses.pop(0)

    async def sleep(self, seconds):
        self.sleeps.append(seconds)

    def client(self):
        return TavilySearch(TAVILY_KEY, transport=httpx.MockTransport(self.handler), sleep=self.sleep,
                            retry=RetryPolicy(max_retries=2, base_delay=0.01, max_delay=30))


TAVILY_OK = {"query": "q", "results": [{"title": "HTTP/3", "url": "https://example.org/h3", "content": "QUIC based", "score": 0.9}]}


async def test_tavily_request_and_results():
    api = TavilyApi(httpx.Response(200, json=TAVILY_OK))
    results = await api.client().search("what is http/3", max_results=3, time_range="month")
    request = api.requests[0]
    assert str(request.url) == "https://api.tavily.com/search" and request.headers["authorization"] == f"Bearer {TAVILY_KEY}"
    assert json.loads(request.content) == {"query": "what is http/3", "max_results": 3, "search_depth": "basic",
                                           "include_answer": False, "time_range": "month"}
    assert results == [{"title": "HTTP/3", "url": "https://example.org/h3", "snippet": "QUIC based"}]


async def test_tavily_errors_and_retries():
    retried = TavilyApi(httpx.Response(429, headers={"retry-after": "3"}, json={"detail": {"error": "slow down"}}),
                        httpx.Response(200, json=TAVILY_OK))
    assert len(await retried.client().search("q")) == 1 and 3 <= retried.sleeps[0] <= 3.25

    used_up = TavilyApi(httpx.Response(432, json={"detail": {"error": "This request exceeds your plan's set usage limit."}}))
    with pytest.raises(ProviderError, match="credits are used up") as caught:
        await used_up.client().search("q")
    assert len(used_up.requests) == 1  # not retried

    bad_key = TavilyApi(httpx.Response(401, json={"detail": {"error": f"Invalid API key {TAVILY_KEY}"}}))
    with pytest.raises(ProviderError, match="authentication failed") as caught:
        await bad_key.client().search("q")
    assert TAVILY_KEY not in str(caught.value)


async def test_tavily_verify_reads_usage_without_searching():
    api = TavilyApi(httpx.Response(200, json={"key": {"usage": 12, "limit": 1000},
                                              "account": {"current_plan": "Researcher", "plan_usage": 12, "plan_limit": 1000}}))
    assert await api.client().verify() == {"plan": "Researcher", "credits_used": 12, "credits_limit": 1000}
    assert api.requests[0].method == "GET" and api.requests[0].url.path == "/usage"


# --- the node ---------------------------------------------------------------------------------


class Provider:
    def __init__(self, name, results=None, error=None):
        self.name, self.results, self.error, self.queries = name, results or [], error, []
        self.is_mock = False

    async def search(self, query, **kwargs):
        self.queries.append((query, kwargs))
        if self.error:
            raise self.error
        return self.results


HITS = [{"title": f"Page {i}", "url": f"https://site{i}.example.com/a", "snippet": f"snippet {i}"} for i in range(1, 4)]


def services(tavily_key=None, **providers):
    settings = ProviderSettings(tavily={"api_key": tavily_key})
    return ExecutionServices(provider_settings=settings, search_providers=providers or None)


async def run_search(services_, **config):
    return await execute_node(node("search", "web_search", **{"query": "  http/3   vs http/2 ", **config}), make_context(services=services_))


async def test_search_results_are_numbered_and_deduplicated():
    ddg = Provider("duckduckgo", HITS + [HITS[0]])
    result = await run_search(services(duckduckgo=ddg), max_results=5, time_range="week")
    assert result.status is NodeStatus.SUCCESS, result.error
    assert [r["position"] for r in result.output["results"]] == [1, 2, 3]
    assert result.output["query"] == "http/3 vs http/2" and result.output["provider_used"] == "duckduckgo"
    assert ddg.queries[0] == ("http/3 vs http/2", {"max_results": 5, "region": "wt-wt", "safe_search": "moderate", "time_range": "week"})


async def test_falls_back_to_tavily_when_duckduckgo_is_rate_limited():
    ddg = Provider("duckduckgo", error=ProviderError("duckduckgo", "DuckDuckGo is rate limiting this server"))
    tavily = Provider("tavily", HITS[:2])
    result = await run_search(services(tavily_key=TAVILY_KEY, duckduckgo=ddg, tavily=tavily))
    assert result.status is NodeStatus.SUCCESS
    assert result.output["provider_used"] == "tavily" and result.output["count"] == 2
    assert result.output["fallback_errors"] == [{"provider": "duckduckgo", "error": "duckduckgo: DuckDuckGo is rate limiting this server"}]
    assert all(r["source"] == "tavily" for r in result.output["results"])


async def test_tavily_falls_back_to_duckduckgo():
    tavily = Provider("tavily", error=ProviderError("tavily", "the plan's credits are used up (HTTP 432)"))
    ddg = Provider("duckduckgo", HITS[:1])
    result = await run_search(services(tavily_key=TAVILY_KEY, duckduckgo=ddg, tavily=tavily), provider="tavily")
    assert result.status is NodeStatus.SUCCESS and result.output["provider_used"] == "duckduckgo"


async def test_without_a_tavily_key_there_is_no_fallback_and_the_error_says_why():
    ddg = Provider("duckduckgo", error=ProviderError("duckduckgo", "timed out"))
    result = await run_search(services(duckduckgo=ddg))
    assert result.status is NodeStatus.FAILED
    assert result.error == "Web search failed: duckduckgo: timed out. Set TAVILY_API_KEY so searches can fall back to Tavily."
    off = await run_search(services(tavily_key=TAVILY_KEY, duckduckgo=ddg, tavily=Provider("tavily", HITS)), fallback=False)
    assert off.status is NodeStatus.FAILED  # fallback turned off


async def test_empty_results_fail_by_default():
    empty = services(duckduckgo=Provider("duckduckgo", []))
    result = await run_search(empty)
    assert result.status is NodeStatus.FAILED and result.error == "No results for 'http/3 vs http/2'"
    lenient = await run_search(empty, fail_when_empty=False)
    assert lenient.status is NodeStatus.SUCCESS and lenient.output["count"] == 0


async def test_fetched_pages_go_through_the_ssrf_guard():
    hits = [{"title": "Good", "url": "https://news.example.com/story", "snippet": "s"},
            {"title": "Internal", "url": "http://169.254.169.254/latest/meta-data", "snippet": "s"}]
    article = "<html><head><title>Story</title></head><body><article><p>" + "The QUIC protocol runs over UDP. " * 20 + "</p></article></body></html>"

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, text=article)

    svc = ExecutionServices(provider_settings=ProviderSettings(), search_providers={"duckduckgo": Provider("duckduckgo", hits)},
                            http_transport=httpx.MockTransport(handler), allow_private_network=False)
    result = await run_search(svc, fetch_pages=2, max_page_chars=300)
    good, internal = result.output["results"]
    assert good["page_text"].startswith("The QUIC protocol runs over UDP.") and len(good["page_text"]) <= 300
    assert "page_text" not in internal and "Blocked request to '169.254.169.254'" in internal["page_error"]


def test_tavily_as_the_provider_needs_a_key():
    graph = WorkflowGraph(nodes=[node("s", "web_search", query="x", provider="tavily")])
    issues = validate_workflow(graph, services=ExecutionServices(provider_settings=ProviderSettings()))
    assert [i.code for i in issues] == [IssueCode.AUTH_MISSING] and "TAVILY_API_KEY" in issues[0].message
    ddg = WorkflowGraph(nodes=[node("s", "web_search", query="x")])
    assert validate_workflow(ddg, services=ExecutionServices(provider_settings=ProviderSettings())) == []


# --- the custom OpenAI-compatible provider -------------------------------------------------------


@pytest.mark.parametrize("url", ["http://localhost:8000/v1", "http://10.1.2.3/v1", "http://169.254.169.254/v1", "http://ollama:11434/v1"])
def test_custom_base_url_must_be_public(url):
    with pytest.raises(ProviderError, match="Blocked request to"):
        OpenAICompatibleProvider("custom", base_url=url)
    settings = ProviderSettings(custom={"base_url": url, "model": "m"})
    with pytest.raises(ProviderError, match="Blocked request to"):
        get_llm_provider("custom", settings)


def test_custom_connections_are_guarded_and_can_be_allowed_for_development():
    provider = OpenAICompatibleProvider("custom", base_url="https://llm.example.com/v1", api_key="k")
    pool = provider._client._client._transport._pool
    assert isinstance(pool._network_backend, GuardedNetworkBackend)  # every connection is checked, redirects too
    local = get_llm_provider("custom", ProviderSettings(custom={"base_url": "http://localhost:1234/v1", "model": "m"},
                                                        allow_private_network=True))
    assert local.base_url == "http://localhost:1234/v1"


def test_custom_needs_a_base_url_and_model():
    assert not ProviderSettings().has_credentials("custom")
    assert not ProviderSettings(custom={"base_url": "https://x.example.com/v1"}).has_credentials("custom")
    assert ProviderSettings(custom={"base_url": "https://x.example.com/v1", "model": "m"}).has_credentials("custom")
    graph = WorkflowGraph(nodes=[node("c", "custom_llm", user_prompt="hi")])
    issues = validate_workflow(graph, services=ExecutionServices(provider_settings=ProviderSettings()))
    assert "CUSTOM_OPENAI_BASE_URL" in issues[0].message


def test_new_providers_are_in_every_llm_node_and_the_fallback_chain():
    from flowforge_engine import get_node_definition

    schema = get_node_definition("gemini").config_schema
    assert schema.model_validate({"user_prompt": "x", "fallback": ["mistral", "cerebras:gpt-oss-120b", "custom"]})
    providers = schema.model_json_schema()["properties"]["provider"]["enum"]
    assert {"mistral", "cerebras", "custom"} <= set(providers)
    mistral = get_llm_provider("mistral", ProviderSettings(mistral={"api_key": "m"}))
    cerebras = get_llm_provider("cerebras", ProviderSettings(cerebras={"api_key": "c"}))
    assert (mistral.base_url, cerebras.base_url) == ("https://api.mistral.ai/v1", "https://api.cerebras.ai/v1")


# --- Web Page fail_on_error ----------------------------------------------------------------------


async def test_web_page_can_report_instead_of_failing():
    svc = ExecutionServices(provider_settings=ProviderSettings(testing=True),
                            http_transport=httpx.MockTransport(lambda r: httpx.Response(403, text="forbidden")))
    strict = await execute_node(node("p", "web_page", url="https://paywall.example.com/x"), make_context(services=svc))
    assert strict.status is NodeStatus.FAILED and "HTTP 403" in strict.error
    lenient = await execute_node(node("p", "web_page", url="https://paywall.example.com/x", fail_on_error=False), make_context(services=svc))
    assert lenient.status is NodeStatus.SUCCESS
    assert lenient.output["text"] == "" and "HTTP 403" in lenient.output["error"]
