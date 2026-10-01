"""Knowledge bases: documents split into chunks, embedded, and searched by meaning.

The engine never touches a database: the API gives each run a KnowledgeStore scoped to the
run's owner (Postgres + pgvector), and the knowledge nodes and the ingestion task go through
it. Everything else lives here and is shared by both: reading a file's text (the PDF text
layer, OCR for scanned pages and images, plain text), chunking, and embedding.

Every knowledge base stores vectors of EMBEDDING_DIMENSIONS numbers, whichever provider
made them: Gemini and OpenAI are asked for that size, longer vectors are truncated and
re-normalized (both models are trained so that a prefix is still a good embedding), and a
model that returns fewer numbers is refused. A knowledge base is embedded by one
provider/model, and its queries must use the same one: vectors from different models
aren't comparable.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

from flowforge_engine.errors import EngineError, ProviderError

logger = logging.getLogger(__name__)
from flowforge_engine.files import IMAGE_TYPES, PDF, StoredFile

EMBEDDING_DIMENSIONS = 768
# Providers with an embeddings API here ("mock": hashed bag of words, for tests and demos).
EMBEDDING_PROVIDERS = ("gemini", "openai", "ollama", "mock")
EMBED_BATCH = 100
# How often one batch may wait out a rate limit before ingestion gives up.
RATE_LIMIT_RETRIES = 5
# Longest single wait for a rate limit: in the background task, and inside a run.
INGEST_RATE_LIMIT_WAIT = 120.0
NODE_RATE_LIMIT_WAIT = 60.0
DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 150
TEXT_TYPES = frozenset({"text/plain"})
KNOWLEDGE_TYPES = frozenset({PDF, *IMAGE_TYPES, *TEXT_TYPES})


class KnowledgeError(EngineError):
    """A knowledge base or document problem a node reports as its failure."""


class KnowledgeBaseNotFound(KnowledgeError):
    pass


# --- Chunking ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    # Character offsets in the source text (of `page` when there is one).
    start: int
    end: int
    page: int | None = None

    def metadata(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "page": self.page}


# Where a chunk would rather end, best first: a paragraph, a line, a sentence, a word.
_BREAKS = (re.compile(r"\n\s*\n"), re.compile(r"\n"), re.compile(r"[.!?;:](?=\s)"), re.compile(r"\s"))


def check_chunking(size: int, overlap: int) -> None:
    if size < 50:
        raise ValueError("chunk_size must be at least 50 characters")
    if not 0 <= overlap < size:
        raise ValueError("chunk_overlap must be at least 0 and smaller than chunk_size")


def _break_at(text: str, start: int, limit: int) -> int:
    """The best end for a chunk starting at `start` and ending by `limit`: the last break of
    the best kind in the second half of the window, else `limit` (a hard cut)."""
    floor = start + (limit - start) // 2
    window = text[floor:limit]
    for pattern in _BREAKS:
        ends = [m.end() for m in pattern.finditer(window)]
        if ends:
            return floor + ends[-1]
    return limit


def chunk_text(
    text: str, size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_CHUNK_OVERLAP, *,
    page: int | None = None, first_index: int = 0,
) -> list[Chunk]:
    """Split `text` into chunks of at most `size` characters, ending at paragraph, line,
    sentence, or word boundaries when one falls in a chunk's second half. Consecutive chunks
    share about `overlap` characters, starting on a word, so a sentence cut at a boundary
    still appears whole in one of them. Whitespace-only pieces are dropped."""
    check_chunking(size, overlap)
    chunks: list[Chunk] = []
    start, length = 0, len(text)
    while start < length:
        while start < length and text[start].isspace():
            start += 1
        if start >= length:
            break
        limit = min(start + size, length)
        end = limit if limit == length else _break_at(text, start, limit)
        piece = text[start:end].rstrip()
        if piece:
            chunks.append(Chunk(first_index + len(chunks), piece, start, start + len(piece), page))
        if end >= length:
            break
        # Step back by the overlap, then forward to the next word so no chunk starts mid-word.
        nxt = max(end - overlap, start + 1)
        if overlap and 0 < nxt < end and not text[nxt - 1].isspace():
            space = re.search(r"\s", text[nxt:end])
            nxt = nxt + space.end() if space else end
        start = nxt
    return chunks


def chunk_pages(
    pages: list[tuple[int | None, str]], size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_CHUNK_OVERLAP
) -> list[Chunk]:
    """Chunk each page on its own (so every chunk can cite one page), numbering continuously."""
    chunks: list[Chunk] = []
    for page, text in pages:
        chunks += chunk_text(text, size, overlap, page=page, first_index=len(chunks))
    return chunks


# --- Embedding ---------------------------------------------------------------------------


def normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm:
        raise ValueError("the embedding is all zeros")
    return [v / norm for v in vector]


def fit_dimensions(vector: list[float], dimensions: int) -> list[float]:
    """`vector` with exactly `dimensions` numbers, unit length."""
    if len(vector) < dimensions:
        raise ValueError(
            f"the model returned {len(vector)}-number vectors; knowledge bases need at least {dimensions} "
            "(use gemini-embedding-*, text-embedding-3-*, or nomic-embed-text)"
        )
    return normalize(vector[:dimensions])


async def _embed_batch(
    provider: Any, part: list[str], model: str | None, dimensions: int, task: str | None, rate_limit_wait: float,
) -> list[list[float]]:
    """One batch; a rate limit (429) whose requested wait is at most `rate_limit_wait`
    seconds is waited out and the batch sent again (up to RATE_LIMIT_RETRIES times)."""
    many = getattr(provider, "embed_many", None)
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            if many is not None:
                return await many(part, model, dimensions, task)
            return [await provider.embed(text, model) for text in part]
        except ProviderError as exc:
            wait = exc.retry_after if exc.retry_after is not None else 60.0
            if exc.status_code != 429 or wait > rate_limit_wait or attempt == RATE_LIMIT_RETRIES:
                raise
            logger.info("embedding rate limited; waiting", extra={"provider": exc.provider, "seconds": round(wait, 1)})
            await asyncio.sleep(wait + 1)
    raise AssertionError("unreachable")


async def embed_texts(
    provider: Any, texts: list[str], *, model: str | None = None, dimensions: int = EMBEDDING_DIMENSIONS,
    task: str | None = None, batch: int = EMBED_BATCH, rate_limit_wait: float = 0,
) -> list[list[float]]:
    """Embed `texts` in batches (one request each where the provider has `embed_many`),
    returning unit vectors of `dimensions` numbers. `task` is "document" or "query".

    Free tiers count every text against a per-minute quota (Gemini: 100 a minute), so a
    long document hits it; with `rate_limit_wait` the provider's requested wait (up to that
    many seconds) is honoured instead of failing. Ingestion uses it; searches don't."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch):
        part = texts[start:start + batch]
        raw = await _embed_batch(provider, part, model, dimensions, task, rate_limit_wait)
        if len(raw) != len(part):
            raise ProviderError(getattr(provider, "name", "embeddings"), f"got {len(raw)} vectors for {len(part)} texts")
        try:
            vectors += [fit_dimensions(list(v), dimensions) for v in raw]
        except ValueError as exc:
            raise ProviderError(getattr(provider, "name", "embeddings"), str(exc)) from None
    return vectors


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norms = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norms if norms else 0.0


# --- Reading a document's text -----------------------------------------------------------


def extract_text(stored: StoredFile, *, ocr_language: str = "eng", cancel: threading.Event | None = None) -> tuple[list[tuple[int | None, str]], str]:
    """(pages as (page number, text), how it was read) for a PDF, an image, or plain text.

    PDFs use each page's text layer and OCR only the pages without one; images are OCR'd.
    Runs in a worker thread (it's CPU-bound); `cancel` stops it at the next page."""
    cancel = cancel or threading.Event()
    if stored.content_type in TEXT_TYPES:
        return [(None, stored.path.read_text(encoding="utf-8", errors="replace"))], "text"
    if stored.content_type not in KNOWLEDGE_TYPES:
        raise KnowledgeError(
            f"'{stored.filename}' is {stored.content_type}; knowledge bases take PDFs, images, and plain text"
        )
    from flowforge_engine.nodes.documents import OCRConfig, _run_ocr

    config = OCRConfig(file=stored.id, language=ocr_language, max_pages=500, prefer_text_layer=True)
    data = _run_ocr(stored, config, cancel)
    methods = {p.get("method", "ocr") for p in data["pages"]}
    method = "text_layer" if methods == {"text_layer"} else "ocr" if methods == {"ocr"} else "mixed"
    return [(p["page"], p["text"]) for p in data["pages"]], method


# --- Stores ------------------------------------------------------------------------------


@dataclass(frozen=True)
class KnowledgeBaseInfo:
    id: str
    name: str
    embedding_provider: str
    embedding_model: str | None
    dimensions: int = EMBEDDING_DIMENSIONS
    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP


@dataclass(frozen=True)
class ChunkHit:
    chunk_id: str
    document_id: str
    filename: str
    chunk_index: int
    content: str
    score: float
    page: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class KnowledgeStore(Protocol):
    async def get(self, ref: str) -> KnowledgeBaseInfo:
        """A knowledge base by id or name. Raises KnowledgeBaseNotFound."""
        ...

    async def search(self, kb: KnowledgeBaseInfo, embedding: list[float], top_k: int) -> list[ChunkHit]:
        """The `top_k` chunks closest to `embedding` (cosine), best first, from ready documents."""
        ...

    async def add_document(self, kb: KnowledgeBaseInfo, stored: StoredFile) -> str:
        """Record a new document (status "processing") for `stored`; returns its id."""
        ...

    async def save_chunks(
        self, document_id: str, chunks: list[Chunk], embeddings: list[list[float]], *, char_count: int, method: str
    ) -> None:
        """Store the chunks and mark the document ready."""
        ...

    async def fail_document(self, document_id: str, error: str) -> None:
        ...


@dataclass
class _MemoryDocument:
    id: str
    kb_id: str
    filename: str
    status: str = "processing"
    error: str | None = None
    chunks: list[tuple[Chunk, list[float]]] = field(default_factory=list)


class MemoryKnowledgeStore:
    """In-process store (tests and one-off runs)."""

    def __init__(self, bases: list[KnowledgeBaseInfo] | None = None) -> None:
        self.bases = {kb.id: kb for kb in bases or []}
        self.documents: dict[str, _MemoryDocument] = {}

    async def get(self, ref: str) -> KnowledgeBaseInfo:
        for kb in self.bases.values():
            if ref in (kb.id, kb.name):
                return kb
        raise KnowledgeBaseNotFound(f"Knowledge base '{ref}' was not found")

    async def search(self, kb: KnowledgeBaseInfo, embedding: list[float], top_k: int) -> list[ChunkHit]:
        hits = [
            ChunkHit(f"{doc.id}:{chunk.index}", doc.id, doc.filename, chunk.index, chunk.text,
                     round(cosine(embedding, vector), 6), chunk.page)
            for doc in self.documents.values() if doc.kb_id == kb.id and doc.status == "ready"
            for chunk, vector in doc.chunks
        ]
        return sorted(hits, key=lambda h: -h.score)[:top_k]

    async def add_document(self, kb: KnowledgeBaseInfo, stored: StoredFile) -> str:
        doc_id = f"doc-{len(self.documents) + 1}"
        self.documents[doc_id] = _MemoryDocument(doc_id, kb.id, stored.filename)
        return doc_id

    async def save_chunks(
        self, document_id: str, chunks: list[Chunk], embeddings: list[list[float]], *, char_count: int, method: str
    ) -> None:
        doc = self.documents[document_id]
        doc.chunks, doc.status = list(zip(chunks, embeddings)), "ready"

    async def fail_document(self, document_id: str, error: str) -> None:
        doc = self.documents[document_id]
        doc.status, doc.error = "failed", error


# --- Ingestion ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestResult:
    document_id: str
    filename: str
    chunk_count: int
    char_count: int
    pages: int
    method: str


async def ingest_document(
    services: Any, store: KnowledgeStore, kb: KnowledgeBaseInfo, stored: StoredFile, document_id: str,
    *, ocr_language: str = "eng", rate_limit_wait: float = INGEST_RATE_LIMIT_WAIT,
) -> IngestResult:
    """Read, chunk, embed, and store one document; on any failure the document is marked
    failed (with the reason) and KnowledgeError is raised."""
    try:
        pages, method = await asyncio.to_thread(extract_text, stored, ocr_language=ocr_language)
        char_count = sum(len(text) for _, text in pages)
        chunks = chunk_pages(pages, kb.chunk_size, kb.chunk_overlap)
        if not chunks:
            raise KnowledgeError(f"No text found in '{stored.filename}'")
        provider = services.llm(kb.embedding_provider)
        vectors = await embed_texts(
            provider, [c.text for c in chunks], model=kb.embedding_model, dimensions=kb.dimensions, task="document",
            rate_limit_wait=rate_limit_wait,
        )
        await store.save_chunks(document_id, chunks, vectors, char_count=char_count, method=method)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, (KnowledgeError, ProviderError, ValueError)) else f"{type(exc).__name__}: {exc}"
        await store.fail_document(document_id, reason[:2000])
        raise KnowledgeError(reason) from exc
    return IngestResult(document_id, stored.filename, len(chunks), char_count, len(pages), method)


async def retrieve(
    services: Any, store: KnowledgeStore, kb: KnowledgeBaseInfo, query: str, top_k: int
) -> list[ChunkHit]:
    provider = services.llm(kb.embedding_provider)
    [vector] = await embed_texts(provider, [query], model=kb.embedding_model, dimensions=kb.dimensions, task="query")
    return await store.search(kb, vector, top_k)


def context_block(hits: list[dict[str, Any]]) -> str:
    """Numbered sources for an LLM prompt: "[1] report.pdf, page 3\\n<text>", one per hit."""
    parts = []
    for number, hit in enumerate(hits, start=1):
        where = hit.get("filename") or "document"
        if hit.get("page"):
            where += f", page {hit['page']}"
        parts.append(f"[{number}] {where}\n{hit.get('content', '')}")
    return "\n\n".join(parts)
