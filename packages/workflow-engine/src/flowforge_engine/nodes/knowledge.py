"""Knowledge nodes: add documents to a knowledge base, chunk, embed, retrieve, rerank.

Add Document and Retriever work on the run owner's knowledge bases (context.services.knowledge,
pgvector in the API); Chunker and Embedding work on any text. The shared pieces (reading
text, chunking, embedding, ingestion) live in flowforge_engine.knowledge, which the API's
upload-and-ingest task uses too.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from flowforge_engine.errors import ProviderError
from flowforge_engine.files import FileNotAvailable
from flowforge_engine.knowledge import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    EMBEDDING_DIMENSIONS,
    KNOWLEDGE_TYPES,
    NODE_RATE_LIMIT_WAIT,
    KnowledgeError,
    check_chunking,
    chunk_text,
    context_block,
    embed_texts,
    ingest_document,
    retrieve,
)
from flowforge_engine.models import GraphNode, NodeContext, NodeResult
from flowforge_engine.nodes.ai import LLMChainFailed, generate_with_fallback
from flowforge_engine.nodes.documents import FILE_DESCRIPTION, FILE_FIELD, FileRef, LLMChainConfig, _LLMDocumentNode, _open_file
from flowforge_engine.nodes.structured import check_reply, retry_request
from flowforge_engine.registry import NodeConfig, NodeDefinition, register_node

logger = logging.getLogger(__name__)

KB_DESCRIPTION = "The knowledge base's name (as shown on the Knowledge Bases page) or id."
EmbeddingProviderName = Literal["gemini", "openai", "ollama", "mock"]


def _hit_rows(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hits numbered for citing: rank and "[n]" follow the list order."""
    return [{**hit, "rank": n, "citation": f"[{n}]"} for n, hit in enumerate(hits, start=1)]


# --- Add Document ------------------------------------------------------------------------


class AddDocumentConfig(NodeConfig):
    knowledge_base: str = Field(min_length=1, description=KB_DESCRIPTION)
    file: FileRef = Field(description=FILE_DESCRIPTION + " PDF, image, or plain text.", json_schema_extra=FILE_FIELD)
    ocr_language: str = Field(
        default="eng", pattern=r"^[a-z_]{3,}(\+[a-z_]{3,})*$",
        description="Tesseract language(s) for scanned pages and images, e.g. eng or eng+hin.",
    )


class AddDocumentResult(BaseModel):
    document_id: str
    knowledge_base: str
    knowledge_base_id: str
    filename: str
    status: str
    chunk_count: int
    char_count: int
    pages: int
    method: str


@register_node("kb_add_document")
class AddDocumentNode(NodeDefinition[AddDocumentConfig]):
    category = "knowledge"
    label = "Knowledge Base: Add Document"
    description = "Reads a file (text layer, OCR for scans), splits it into chunks, embeds them, and stores them in a knowledge base."
    icon = "library"
    config_schema = AddDocumentConfig
    output_schema = AddDocumentResult
    # Reading the file is CPU work (OCR); embedding is one request per 100 chunks.
    queue = "ocr"

    async def execute(self, context: NodeContext, config: AddDocumentConfig) -> NodeResult:
        try:
            store = context.services.knowledge
            kb = await store.get(config.knowledge_base.strip())
            stored = await _open_file(context, config.file, KNOWLEDGE_TYPES)
        except (KnowledgeError, FileNotAvailable) as exc:
            return NodeResult.fail(str(exc))
        document_id = await store.add_document(kb, stored)
        try:
            result = await ingest_document(
                context.services, store, kb, stored, document_id, ocr_language=config.ocr_language,
                rate_limit_wait=NODE_RATE_LIMIT_WAIT,
            )
        except KnowledgeError as exc:
            return NodeResult.fail(f"Couldn't add '{stored.filename}' to '{kb.name}': {exc}", document_id=document_id)
        logger.info(
            "document added to knowledge base",
            extra={"node_id": context.node_id, "knowledge_base_id": kb.id, "chunks": result.chunk_count},
        )
        return NodeResult.ok(
            document_id=document_id, knowledge_base=kb.name, knowledge_base_id=kb.id, filename=stored.filename,
            status="ready", chunk_count=result.chunk_count, char_count=result.char_count, pages=result.pages,
            method=result.method,
        )


# --- Chunker -----------------------------------------------------------------------------


class ChunkerConfig(NodeConfig):
    text: str = Field(min_length=1, description="The text to split, e.g. {{pdf_extract.text}}.")
    chunk_size: int = Field(default=DEFAULT_CHUNK_SIZE, ge=50, le=20_000, description="Most characters in a chunk.")
    chunk_overlap: int = Field(
        default=DEFAULT_CHUNK_OVERLAP, ge=0, le=5_000, description="Characters consecutive chunks share."
    )

    @model_validator(mode="after")
    def _overlap_fits(self) -> ChunkerConfig:
        check_chunking(self.chunk_size, self.chunk_overlap)
        return self


class ChunkerResult(BaseModel):
    chunks: list[dict[str, Any]]
    count: int


@register_node("chunker")
class ChunkerNode(NodeDefinition[ChunkerConfig]):
    category = "knowledge"
    label = "Chunker"
    description = "Splits text into overlapping chunks at paragraph, sentence, or word boundaries."
    icon = "scissors"
    config_schema = ChunkerConfig
    output_schema = ChunkerResult

    async def execute(self, context: NodeContext, config: ChunkerConfig) -> NodeResult:
        chunks = chunk_text(config.text, config.chunk_size, config.chunk_overlap)
        return NodeResult.ok(
            chunks=[{"index": c.index, "text": c.text, "start": c.start, "end": c.end} for c in chunks],
            count=len(chunks),
        )


# --- Embedding ---------------------------------------------------------------------------


class EmbeddingConfig(NodeConfig):
    text: str | list[str] = Field(
        description="A text, or a list of texts (e.g. the chunk texts) to embed in one go."
    )
    provider: EmbeddingProviderName = Field(default="gemini", description="Who makes the vectors. 'mock' hashes words (no key).")
    model: str | None = Field(default=None, description="Blank uses the provider's embedding model.")
    dimensions: int = Field(default=EMBEDDING_DIMENSIONS, ge=8, le=3072, description="Numbers per vector.")
    task: Literal["document", "query"] = Field(
        default="document", description="Gemini embeds documents and search queries slightly differently."
    )


class EmbeddingResult(BaseModel):
    embedding: list[float] | None
    embeddings: list[list[float]]
    count: int
    dimensions: int
    provider: str
    model: str | None
    mock: bool


@register_node("embedding")
class EmbeddingNode(NodeDefinition[EmbeddingConfig]):
    category = "knowledge"
    label = "Embedding"
    description = "Turns text into vectors (lists of numbers) that are close together when the texts mean similar things."
    icon = "binary"
    config_schema = EmbeddingConfig
    output_schema = EmbeddingResult
    queue = "llm"

    def required_providers(self, node: GraphNode) -> list[tuple[str, str]]:
        provider = node.config.get("provider", "gemini")
        return [(provider, "provider")] if provider in ("gemini", "openai", "ollama") else []

    async def execute(self, context: NodeContext, config: EmbeddingConfig) -> NodeResult:
        texts = [config.text] if isinstance(config.text, str) else [str(t) for t in config.text]
        if not texts or not any(t.strip() for t in texts):
            return NodeResult.fail("Nothing to embed: the text is empty")
        try:
            provider = context.services.llm(config.provider)
            vectors = await embed_texts(
                provider, texts, model=config.model, dimensions=config.dimensions, task=config.task
            )
        except ProviderError as exc:
            return NodeResult.fail(str(exc))
        return NodeResult.ok(
            embedding=vectors[0] if isinstance(config.text, str) else None, embeddings=vectors, count=len(vectors),
            dimensions=config.dimensions, provider=config.provider, model=config.model,
            mock=bool(getattr(provider, "is_mock", False)),
        )


# --- Retriever ---------------------------------------------------------------------------


class RetrieverConfig(NodeConfig):
    knowledge_base: str = Field(min_length=1, description=KB_DESCRIPTION)
    query: str = Field(min_length=1, description="What to look for, e.g. {{input.question}}.")
    top_k: int = Field(default=5, ge=1, le=50, description="How many chunks to return.")
    min_score: float = Field(
        default=0.0, ge=-1, le=1, description="Drop chunks less similar than this (cosine, -1 to 1)."
    )


class RetrieverResult(BaseModel):
    results: list[dict[str, Any]]
    context: str
    count: int
    knowledge_base: str
    query: str


@register_node("retriever")
class RetrieverNode(NodeDefinition[RetrieverConfig]):
    category = "knowledge"
    label = "Retriever"
    description = "Finds the chunks of a knowledge base closest in meaning to a query, with their source and score."
    icon = "search-code"
    config_schema = RetrieverConfig
    output_schema = RetrieverResult
    queue = "llm"

    async def execute(self, context: NodeContext, config: RetrieverConfig) -> NodeResult:
        try:
            store = context.services.knowledge
            kb = await store.get(config.knowledge_base.strip())
            hits = await retrieve(context.services, store, kb, config.query, config.top_k)
        except (KnowledgeError, ProviderError) as exc:
            return NodeResult.fail(str(exc))
        rows = _hit_rows([h.as_dict() for h in hits if h.score >= config.min_score])
        return NodeResult.ok(
            results=rows, context=context_block(rows), count=len(rows), knowledge_base=kb.name, query=config.query
        )


# --- Reranker ----------------------------------------------------------------------------

RERANK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["scores"],
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "score"],
                "properties": {"id": {"type": "integer"}, "score": {"type": "number"}},
            },
        }
    },
}
RERANK_PASSAGE_CHARS = 1500


def rerank_prompt(query: str, passages: list[str]) -> str:
    listed = "\n\n".join(f"<passage id={n}>\n{p[:RERANK_PASSAGE_CHARS]}\n</passage>" for n, p in enumerate(passages, 1))
    return (
        "Rate how well each passage answers the question, from 0 (irrelevant) to 10 (answers it directly). "
        "Judge only what the passage says.\n\n"
        f"Question: {query}\n\n{listed}\n\n"
        'Reply with JSON only: {"scores": [{"id": 1, "score": 7}, ...]} with one entry per passage.'
    )


def apply_scores(results: list[dict[str, Any]], scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`results` ordered by the model's scores (ties keep their retrieval order); passages
    the model didn't score get 0."""
    by_id: dict[int, float] = {}
    for entry in scores:
        try:
            by_id.setdefault(int(entry["id"]), float(entry["score"]))
        except (KeyError, TypeError, ValueError):
            continue
    scored = [
        {**row, "retrieval_rank": row.get("rank", n), "rerank_score": by_id.get(n, 0.0)}
        for n, row in enumerate(results, start=1)
    ]
    return sorted(scored, key=lambda r: (-r["rerank_score"], r["retrieval_rank"]))


class RerankerConfig(LLMChainConfig):
    query: str = Field(min_length=1, description="The question the results should answer, e.g. {{input.question}}.")
    results: Any = Field(description="The Retriever's results, e.g. {{retriever.results}}.")
    top_n: int = Field(default=3, ge=1, le=50, description="How many to keep after reordering.")
    temperature: float = Field(default=0.0, ge=0, le=2)
    keep_order_on_failure: bool = Field(
        default=True,
        description="If the model's reply can't be used, keep the retrieval order (reported as reranked: false) instead of failing.",
    )


class RerankerResult(BaseModel):
    results: list[dict[str, Any]]
    context: str
    count: int
    reranked: bool
    warning: str | None = None
    provider: str
    provider_used: str | None
    model: str | None
    mock: bool


@register_node("reranker")
class RerankerNode(_LLMDocumentNode, NodeDefinition[RerankerConfig]):
    category = "knowledge"
    label = "Reranker"
    description = "Has an LLM score each retrieved chunk against the question, then keeps the best ones in order."
    icon = "list-ordered"
    config_schema = RerankerConfig
    output_schema = RerankerResult
    queue = "llm"

    async def execute(self, context: NodeContext, config: RerankerConfig) -> NodeResult:
        results = config.results
        if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
            return NodeResult.fail("results must be a list of the Retriever's results, e.g. {{retriever.results}}")
        meta: dict[str, Any] = {"provider": config.provider, "provider_used": None, "model": None, "mock": False}

        def finish(rows: list[dict[str, Any]], reranked: bool, warning: str | None = None) -> NodeResult:
            kept = _hit_rows(rows[: config.top_n])
            return NodeResult.ok(
                results=kept, context=context_block(kept), count=len(kept), reranked=reranked, warning=warning, **meta
            )

        if not results:
            return finish([], reranked=False)
        request = rerank_prompt(config.query, [str(r.get("content", "")) for r in results])
        problems: list[str] = []
        for attempt in (1, 2):
            try:
                answer = await generate_with_fallback(
                    context, provider=config.provider, model=config.model, fallback=config.fallback,
                    system_prompt="You are a careful relevance judge. You reply with JSON only.",
                    user_prompt=request if attempt == 1 else retry_request(request, problems),
                    temperature=config.temperature, max_tokens=config.max_tokens,
                )
            except LLMChainFailed as exc:
                if config.keep_order_on_failure:
                    return finish(results, reranked=False, warning=str(exc))
                return NodeResult.fail(str(exc), fallback_errors=exc.errors)
            meta.update(provider_used=answer.provider_used, model=answer.model, mock=answer.mock)
            data, problems = check_reply(answer.text, RERANK_SCHEMA)
            if not problems:
                return finish(apply_scores(results, data["scores"]), reranked=True)
        warning = f"The model's scores couldn't be read: {'; '.join(problems[:3])}"
        if config.keep_order_on_failure:
            return finish(results, reranked=False, warning=warning)
        return NodeResult.fail(warning)
