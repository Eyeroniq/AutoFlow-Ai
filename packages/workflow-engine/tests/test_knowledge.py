"""Knowledge bases: chunking boundaries, embedding dimensions and batching, retrieval
ranking, reranker reordering, and the Add Document / Chunker / Embedding nodes."""

import json
import math

import pytest

from flowforge_engine import (
    ExecutionServices,
    LocalFileStore,
    NodeStatus,
    ProviderSettings,
    execute_node,
)
from flowforge_engine.errors import ProviderError
from flowforge_engine.knowledge import (
    EMBEDDING_DIMENSIONS,
    KnowledgeBaseInfo,
    MemoryKnowledgeStore,
    chunk_pages,
    chunk_text,
    context_block,
    embed_texts,
    fit_dimensions,
)
from flowforge_engine.nodes.knowledge import apply_scores, rerank_prompt
from flowforge_engine.providers import MockLLMProvider
from flowforge_engine.testing import make_context, node

POLICY = (
    "Hotel stays are reimbursed up to 150 USD per night in most cities, and 250 USD in Tokyo.\n\n"
    "Meal allowance is 60 USD per day while travelling.\n\n"
    "Laptops are replaced every three years; request one through the IT portal."
)

# --- Chunking ----------------------------------------------------------------------------


def test_short_text_is_one_chunk():
    [chunk] = chunk_text("  Just one sentence.  ", size=100, overlap=10)
    assert chunk.text == "Just one sentence." and chunk.index == 0
    assert (chunk.start, chunk.end) == (2, 20)


def test_chunks_respect_size_and_prefer_paragraph_breaks():
    paragraphs = [f"Paragraph {n}. " + "word " * 30 for n in range(6)]
    text = "\n\n".join(p.strip() for p in paragraphs)
    chunks = chunk_text(text, size=400, overlap=0)
    assert len(chunks) > 1
    assert all(len(c.text) <= 400 for c in chunks)
    # Every chunk but the last ends where a paragraph ended.
    for chunk in chunks[:-1]:
        assert text[chunk.end:chunk.end + 2] == "\n\n" or text[chunk.end:].lstrip().startswith("Paragraph")
    # Nothing is lost: every word is in some chunk.
    assert " ".join(c.text for c in chunks).split() == text.split()


def test_offsets_point_at_the_source_text():
    text = "Alpha beta gamma. " * 40
    for chunk in chunk_text(text, size=120, overlap=30):
        assert text[chunk.start:chunk.end] == chunk.text


def test_overlap_repeats_the_end_of_the_previous_chunk_starting_on_a_word():
    text = " ".join(f"w{n}" for n in range(300))
    chunks = chunk_text(text, size=100, overlap=30)
    for before, after in zip(chunks, chunks[1:]):
        assert after.start < before.end  # they overlap
        assert text[after.start - 1] == " "  # and the next one starts on a word
        assert before.text.endswith(text[after.start:before.end])


def test_a_word_longer_than_a_chunk_is_cut_hard():
    chunks = chunk_text("x" * 250, size=100, overlap=0)
    assert [len(c.text) for c in chunks] == [100, 100, 50]


def test_whitespace_only_text_has_no_chunks():
    assert chunk_text(" \n\n\t ", size=100, overlap=10) == []


@pytest.mark.parametrize(("size", "overlap"), [(100, 100), (100, -1), (10, 0)])
def test_invalid_chunking_is_refused(size, overlap):
    with pytest.raises(ValueError):
        chunk_text("text", size=size, overlap=overlap)


def test_pages_are_chunked_separately_and_numbered_continuously():
    chunks = chunk_pages([(1, "Page one text. " * 20), (2, "Page two.")], size=120, overlap=20)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert chunks[-1].page == 2 and chunks[-1].text == "Page two."
    assert {c.page for c in chunks[:-1]} == {1}


# --- Embedding dimensions ------------------------------------------------------------------


def test_longer_vectors_are_truncated_and_renormalized():
    vector = fit_dimensions([3.0, 4.0, 12.0, 99.0], 2)
    assert vector == pytest.approx([0.6, 0.8])


def test_shorter_vectors_are_refused():
    with pytest.raises(ValueError, match="2-number vectors"):
        fit_dimensions([0.1, 0.2], 768)


class BatchRecorder:
    """embed_many that records batch sizes and returns over-long vectors."""

    name = "recorder"

    def __init__(self, size=1024):
        self.batches: list[int] = []
        self.calls: list[dict] = []
        self.size = size

    async def embed_many(self, texts, model=None, dimensions=None, task=None):
        self.batches.append(len(texts))
        self.calls.append({"model": model, "dimensions": dimensions, "task": task})
        return [[float(i + 1)] * self.size for i, _ in enumerate(texts)]


async def test_embed_texts_batches_and_fits_dimensions():
    provider = BatchRecorder()
    vectors = await embed_texts(provider, [f"t{n}" for n in range(250)], model="m", dimensions=768, task="document", batch=100)
    assert provider.batches == [100, 100, 50]
    assert provider.calls[0] == {"model": "m", "dimensions": 768, "task": "document"}
    assert len(vectors) == 250 and all(len(v) == 768 for v in vectors)
    assert math.isclose(sum(x * x for x in vectors[0]), 1.0)


async def test_embed_texts_falls_back_to_embed_one_at_a_time():
    class OneAtATime:
        name = "single"

        async def embed(self, text, model=None):
            return [1.0] * 800

    assert len(await embed_texts(OneAtATime(), ["a", "b"], dimensions=768)) == 2


async def test_embed_texts_reports_a_too_small_model_as_a_provider_error():
    with pytest.raises(ProviderError, match="16-number vectors"):
        await embed_texts(BatchRecorder(size=16), ["a"], dimensions=768)


class RateLimitedOnce:
    """Answers 429 (with a requested wait) on the first `failures` calls, then succeeds."""

    name = "limited"

    def __init__(self, failures=1, retry_after=0.01):
        self.failures, self.retry_after, self.calls = failures, retry_after, 0

    async def embed_many(self, texts, model=None, dimensions=None, task=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderError("gemini", "rate limited (HTTP 429)", status_code=429, retryable=True, retry_after=self.retry_after)
        return [[1.0] * 768 for _ in texts]


async def test_ingestion_waits_out_a_rate_limit_and_resends_the_batch(monkeypatch):
    import flowforge_engine.knowledge as knowledge

    waits = []

    async def no_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(knowledge.asyncio, "sleep", no_sleep)
    provider = RateLimitedOnce(failures=2, retry_after=41)
    vectors = await embed_texts(provider, ["a", "b"], rate_limit_wait=120)
    assert len(vectors) == 2 and provider.calls == 3 and waits == [42, 42]


async def test_a_rate_limit_isnt_waited_out_without_permission_or_when_too_long():
    with pytest.raises(ProviderError, match="429"):
        await embed_texts(RateLimitedOnce(), ["a"])  # searches: rate_limit_wait 0
    with pytest.raises(ProviderError, match="429"):
        await embed_texts(RateLimitedOnce(retry_after=300), ["a"], rate_limit_wait=120)


async def test_mock_embeddings_put_texts_sharing_words_closer():
    mock = MockLLMProvider()
    query, near, far = (await mock.embed_many(
        ["hotel price in tokyo", "the hotel price per night in tokyo is 250", "laptops are replaced every three years"],
        dimensions=EMBEDDING_DIMENSIONS,
    ))
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731
    assert len(query) == EMBEDDING_DIMENSIONS
    assert dot(query, near) > dot(query, far)


# --- Store, retrieval, nodes ---------------------------------------------------------------

KB = KnowledgeBaseInfo(id="kb-1", name="Policies", embedding_provider="mock", embedding_model=None, chunk_size=120, chunk_overlap=0)


def kb_services(tmp_path, store=None, **llm):
    files = LocalFileStore()
    services = ExecutionServices(
        provider_settings=ProviderSettings(testing=True), files=files, knowledge=store or MemoryKnowledgeStore([KB]),
        llm_providers=llm or None,
    )
    return services, files


def text_file(tmp_path, files, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return files.add(path, content_type="text/plain")


async def add(services, files, tmp_path, name, body):
    stored = text_file(tmp_path, files, name, body)
    return await execute_node(
        node("add", "kb_add_document", knowledge_base="Policies", file=stored.id), make_context(services=services)
    )


async def test_add_document_reads_chunks_embeds_and_stores(tmp_path):
    services, files = kb_services(tmp_path)
    result = await add(services, files, tmp_path, "policy.txt", POLICY)
    assert result.status == NodeStatus.SUCCESS, result.error
    out = result.output
    assert out["status"] == "ready" and out["knowledge_base"] == "Policies" and out["filename"] == "policy.txt"
    assert out["chunk_count"] >= 2 and out["char_count"] == len(POLICY) and out["method"] == "text"
    stored = services.knowledge.documents[out["document_id"]]
    assert all(len(vector) == EMBEDDING_DIMENSIONS for _, vector in stored.chunks)


async def test_add_document_to_an_unknown_knowledge_base_fails_clearly(tmp_path):
    services, files = kb_services(tmp_path)
    stored = text_file(tmp_path, files, "a.txt", "hello")
    result = await execute_node(node("add", "kb_add_document", knowledge_base="Nope", file=stored.id), make_context(services=services))
    assert result.status == NodeStatus.FAILED and "'Nope' was not found" in result.error


async def test_an_empty_document_is_marked_failed(tmp_path):
    services, files = kb_services(tmp_path)
    result = await add(services, files, tmp_path, "empty.txt", "   \n  ")
    assert result.status == NodeStatus.FAILED and "No text found" in result.error
    [doc] = services.knowledge.documents.values()
    assert doc.status == "failed" and "No text found" in doc.error


async def test_retriever_ranks_the_matching_chunk_first_with_its_source(tmp_path):
    services, files = kb_services(tmp_path)
    await add(services, files, tmp_path, "policy.txt", POLICY)
    await add(services, files, tmp_path, "it.txt", "Printers are on the second floor. Wifi password is on the badge.")
    result = await execute_node(
        node("retriever", "retriever", knowledge_base="kb-1", query="what is the hotel price in tokyo", top_k=3),
        make_context(services=services),
    )
    assert result.status == NodeStatus.SUCCESS, result.error
    rows = result.output["results"]
    assert rows[0]["filename"] == "policy.txt" and "Tokyo" in rows[0]["content"]
    assert [r["rank"] for r in rows] == [1, 2, 3] and rows[0]["citation"] == "[1]"
    assert [r["score"] for r in rows] == sorted((r["score"] for r in rows), reverse=True)
    assert result.output["context"].startswith("[1] policy.txt\n")


async def test_retriever_min_score_drops_weak_matches(tmp_path):
    services, files = kb_services(tmp_path)
    await add(services, files, tmp_path, "policy.txt", POLICY)
    result = await execute_node(
        node("retriever", "retriever", knowledge_base="Policies", query="tokyo hotel", top_k=5, min_score=0.99),
        make_context(services=services),
    )
    assert result.status == NodeStatus.SUCCESS and result.output["count"] == 0 and result.output["context"] == ""


def test_context_block_numbers_sources_with_pages():
    block = context_block([{"filename": "a.pdf", "page": 3, "content": "x"}, {"filename": "b.txt", "content": "y"}])
    assert block == "[1] a.pdf, page 3\nx\n\n[2] b.txt\ny"


async def test_chunker_node_and_its_validation():
    result = await execute_node(node("chunker", "chunker", text="one two three " * 50, chunk_size=100, chunk_overlap=20), make_context())
    assert result.status == NodeStatus.SUCCESS and result.output["count"] == len(result.output["chunks"]) > 1
    bad = await execute_node(node("chunker", "chunker", text="x", chunk_size=100, chunk_overlap=100), make_context())
    assert bad.status == NodeStatus.FAILED and "chunk_overlap" in bad.error


async def test_embedding_node_single_and_list():
    one = await execute_node(node("embedding", "embedding", text="hello", provider="mock", dimensions=64), make_context())
    assert one.status == NodeStatus.SUCCESS and len(one.output["embedding"]) == 64 and one.output["count"] == 1
    many = await execute_node(node("embedding", "embedding", text=["a", "b", "c"], provider="mock"), make_context())
    assert many.output["embedding"] is None and len(many.output["embeddings"]) == 3
    assert all(len(v) == EMBEDDING_DIMENSIONS for v in many.output["embeddings"])


# --- Reranker ------------------------------------------------------------------------------

RESULTS = [
    {"rank": 1, "filename": "a.txt", "content": "Meal allowance is 60 USD per day.", "score": 0.8},
    {"rank": 2, "filename": "b.txt", "content": "Tokyo hotels: up to 250 USD per night.", "score": 0.7},
    {"rank": 3, "filename": "c.txt", "content": "Laptops are replaced every three years.", "score": 0.6},
]


class ScriptedLLM:
    is_mock = False

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def generate(self, system_prompt, user_prompt, model, temperature, max_tokens):
        self.prompts.append(user_prompt)
        return self.replies.pop(0)


def rerank_context(llm):
    return make_context(services=ExecutionServices(provider_settings=ProviderSettings(testing=True), llm_providers={"gemini": llm}))


def test_apply_scores_orders_by_score_and_keeps_ties_in_retrieval_order():
    ordered = apply_scores(RESULTS, [{"id": 2, "score": 9}, {"id": 1, "score": 3}, {"id": 3, "score": 3}])
    assert [r["filename"] for r in ordered] == ["b.txt", "a.txt", "c.txt"]
    assert [r["retrieval_rank"] for r in ordered] == [2, 1, 3]
    missing = apply_scores(RESULTS, [{"id": 3, "score": 1}])
    assert [r["filename"] for r in missing] == ["c.txt", "a.txt", "b.txt"]


def test_rerank_prompt_numbers_the_passages():
    prompt = rerank_prompt("hotel?", ["first", "second"])
    assert "<passage id=1>\nfirst" in prompt and "<passage id=2>\nsecond" in prompt and "Question: hotel?" in prompt


async def test_reranker_reorders_renumbers_and_keeps_top_n():
    llm = ScriptedLLM(json.dumps({"scores": [{"id": 1, "score": 2}, {"id": 2, "score": 10}, {"id": 3, "score": 0}]}))
    result = await execute_node(
        node("reranker", "reranker", query="Tokyo hotel limit?", results=RESULTS, top_n=2), rerank_context(llm)
    )
    assert result.status == NodeStatus.SUCCESS, result.error
    out = result.output
    assert out["reranked"] is True and out["count"] == 2
    assert [r["filename"] for r in out["results"]] == ["b.txt", "a.txt"]
    assert [r["citation"] for r in out["results"]] == ["[1]", "[2]"]
    assert out["results"][0]["retrieval_rank"] == 2 and out["results"][0]["rerank_score"] == 10
    assert out["context"].startswith("[1] b.txt\nTokyo")


async def test_reranker_retries_once_then_keeps_retrieval_order():
    llm = ScriptedLLM("not json", "still not json")
    result = await execute_node(node("reranker", "reranker", query="q", results=RESULTS, top_n=3), rerank_context(llm))
    assert result.status == NodeStatus.SUCCESS
    assert result.output["reranked"] is False and "couldn't be read" in result.output["warning"]
    assert [r["filename"] for r in result.output["results"]] == ["a.txt", "b.txt", "c.txt"]
    assert len(llm.prompts) == 2 and "didn't match the schema" in llm.prompts[1]


async def test_reranker_can_fail_instead():
    llm = ScriptedLLM("nope", "nope")
    result = await execute_node(
        node("reranker", "reranker", query="q", results=RESULTS, keep_order_on_failure=False), rerank_context(llm)
    )
    assert result.status == NodeStatus.FAILED and "couldn't be read" in result.error


async def test_reranker_rejects_results_that_arent_a_list():
    result = await execute_node(node("reranker", "reranker", query="q", results="text"), rerank_context(ScriptedLLM()))
    assert result.status == NodeStatus.FAILED and "must be a list" in result.error
