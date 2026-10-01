"""Knowledge bases in Postgres + pgvector: the engine's KnowledgeStore, CRUD, and ingestion.

Adding a document through the API stores the upload, records the document ("pending"),
and queues `flowforge.ingest_document` on the ocr workers, which read, chunk, and embed it
(flowforge_engine.knowledge.ingest_document) and mark it ready or failed; the page polls
the documents list for the status. The Add Document node does the same work inline in its
run. Searches embed the query with the knowledge base's own model and order chunks by
cosine distance (HNSW index).
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from flowforge_engine.files import FileNotAvailable, StoredFile
from flowforge_engine.knowledge import (
    EMBEDDING_DIMENSIONS,
    Chunk,
    ChunkHit,
    KnowledgeBaseInfo,
    KnowledgeBaseNotFound,
    KnowledgeError,
    ingest_document,
)
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionFactory
from app.models.enums import DocumentStatus
from app.models.file import UploadedFile
from app.models.knowledge import KnowledgeBase, KnowledgeChunk, KnowledgeDocument
from app.models.user import User

logger = logging.getLogger(__name__)

# A document "processing" for longer than this was abandoned (its worker died).
STALE_PROCESSING = timedelta(minutes=30)


def utcnow() -> datetime:
    return datetime.now(UTC)


def source_type(content_type: str) -> str:
    if content_type == "application/pdf":
        return "pdf"
    return "image" if content_type.startswith("image/") else "text"


def kb_info(kb: KnowledgeBase) -> KnowledgeBaseInfo:
    return KnowledgeBaseInfo(
        id=str(kb.id), name=kb.name, embedding_provider=kb.embedding_provider, embedding_model=kb.embedding_model,
        dimensions=kb.dimensions, chunk_size=kb.chunk_size, chunk_overlap=kb.chunk_overlap,
    )


async def owned_kb(db: AsyncSession, owner_id: uuid.UUID, ref: str | uuid.UUID) -> KnowledgeBase | None:
    """A knowledge base of `owner_id` by id or (exact) name."""
    try:
        kb_id = ref if isinstance(ref, uuid.UUID) else uuid.UUID(str(ref))
    except ValueError:
        kb_id = None
    if kb_id is not None:
        found = await db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == kb_id, KnowledgeBase.owner_id == owner_id))
        if found is not None:
            return found
    return await db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == str(ref), KnowledgeBase.owner_id == owner_id))


async def search_chunks(db: AsyncSession, kb_id: uuid.UUID, embedding: list[float], top_k: int) -> list[ChunkHit]:
    distance = KnowledgeChunk.embedding.cosine_distance(embedding)
    rows = await db.execute(
        select(KnowledgeChunk, KnowledgeDocument.filename, distance.label("distance"))
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .where(KnowledgeChunk.knowledge_base_id == kb_id, KnowledgeDocument.status == DocumentStatus.READY)
        .order_by(distance)
        .limit(top_k)
    )
    return [
        ChunkHit(
            chunk_id=str(chunk.id), document_id=str(chunk.document_id), filename=filename,
            chunk_index=chunk.chunk_index, content=chunk.content, score=round(1.0 - float(dist), 6),
            page=(chunk.metadata_json or {}).get("page"),
        )
        for chunk, filename, dist in rows.all()
    ]


class DbKnowledgeStore:
    """The engine's KnowledgeStore for one user's runs: only that user's knowledge bases."""

    def __init__(self, db: AsyncSession, owner_id: uuid.UUID):
        self._db = db
        self._owner_id = owner_id

    async def get(self, ref: str) -> KnowledgeBaseInfo:
        kb = await owned_kb(self._db, self._owner_id, ref)
        if kb is None:
            raise KnowledgeBaseNotFound(f"Knowledge base '{ref}' was not found (create it on the Knowledge Bases page)")
        return kb_info(kb)

    async def search(self, kb: KnowledgeBaseInfo, embedding: list[float], top_k: int) -> list[ChunkHit]:
        return await search_chunks(self._db, uuid.UUID(kb.id), embedding, top_k)

    async def add_document(self, kb: KnowledgeBaseInfo, stored: StoredFile) -> str:
        document = KnowledgeDocument(
            knowledge_base_id=uuid.UUID(kb.id), file_id=uuid.UUID(stored.id), filename=stored.filename,
            status=DocumentStatus.PROCESSING, source_type=source_type(stored.content_type),
        )
        self._db.add(document)
        await self._db.commit()
        return str(document.id)

    async def save_chunks(
        self, document_id: str, chunks: list[Chunk], embeddings: list[list[float]], *, char_count: int, method: str
    ) -> None:
        doc_id = uuid.UUID(document_id)
        document = await self._db.get(KnowledgeDocument, doc_id)
        if document is None:  # deleted while it was being processed
            return
        # A retried ingestion replaces what an earlier attempt stored.
        await self._db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == doc_id))
        self._db.add_all(
            KnowledgeChunk(
                document_id=doc_id, knowledge_base_id=document.knowledge_base_id, chunk_index=chunk.index,
                content=chunk.text, embedding=vector, metadata_json=chunk.metadata(),
            )
            for chunk, vector in zip(chunks, embeddings, strict=True)
        )
        document.status, document.error = DocumentStatus.READY, None
        document.chunk_count, document.char_count, document.method = len(chunks), char_count, method
        await self._db.commit()

    async def fail_document(self, document_id: str, error: str) -> None:
        document = await self._db.get(KnowledgeDocument, uuid.UUID(document_id))
        if document is None:
            return
        document.status, document.error = DocumentStatus.FAILED, error
        await self._db.commit()


# --- CRUD ----------------------------------------------------------------------------------


class KnowledgeConflict(Exception):
    pass


async def list_kbs(db: AsyncSession, owner_id: uuid.UUID) -> list[tuple[KnowledgeBase, int, int]]:
    """(knowledge base, documents, ready chunks), newest first."""
    docs = (
        select(KnowledgeDocument.knowledge_base_id, func.count().label("documents"),
               func.coalesce(func.sum(KnowledgeDocument.chunk_count), 0).label("chunks"))
        .group_by(KnowledgeDocument.knowledge_base_id)
        .subquery()
    )
    rows = await db.execute(
        select(KnowledgeBase, func.coalesce(docs.c.documents, 0), func.coalesce(docs.c.chunks, 0))
        .outerjoin(docs, docs.c.knowledge_base_id == KnowledgeBase.id)
        .where(KnowledgeBase.owner_id == owner_id)
        .order_by(KnowledgeBase.created_at.desc())
    )
    return [(kb, int(d), int(c)) for kb, d, c in rows.all()]


async def kb_counts(db: AsyncSession, kb_id: uuid.UUID) -> tuple[int, int]:
    row = (await db.execute(
        select(func.count(), func.coalesce(func.sum(KnowledgeDocument.chunk_count), 0))
        .where(KnowledgeDocument.knowledge_base_id == kb_id)
    )).one()
    return int(row[0]), int(row[1])


async def create_kb(
    db: AsyncSession, owner_id: uuid.UUID, *, name: str, description: str, embedding_provider: str,
    embedding_model: str | None, chunk_size: int, chunk_overlap: int,
) -> KnowledgeBase:
    if await db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.owner_id == owner_id, KnowledgeBase.name == name)):
        raise KnowledgeConflict(f"You already have a knowledge base named '{name}'")
    kb = KnowledgeBase(
        owner_id=owner_id, name=name, description=description, embedding_provider=embedding_provider,
        embedding_model=embedding_model, dimensions=EMBEDDING_DIMENSIONS, chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    db.add(kb)
    await db.commit()
    return kb


async def rename_kb(db: AsyncSession, kb: KnowledgeBase, *, name: str | None, description: str | None) -> KnowledgeBase:
    if name is not None and name != kb.name:
        taken = await db.scalar(
            select(KnowledgeBase.id).where(KnowledgeBase.owner_id == kb.owner_id, KnowledgeBase.name == name)
        )
        if taken:
            raise KnowledgeConflict(f"You already have a knowledge base named '{name}'")
        kb.name = name
    if description is not None:
        kb.description = description
    await db.commit()
    await db.refresh(kb)  # updated_at is set by the database
    return kb


async def list_documents(db: AsyncSession, kb_id: uuid.UUID) -> list[KnowledgeDocument]:
    return list(await db.scalars(
        select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kb_id)
        .order_by(KnowledgeDocument.created_at.desc(), KnowledgeDocument.id)
    ))


async def add_pending_document(db: AsyncSession, kb: KnowledgeBase, record: UploadedFile) -> KnowledgeDocument:
    document = KnowledgeDocument(
        knowledge_base_id=kb.id, file_id=record.id, filename=record.filename, status=DocumentStatus.PENDING,
        source_type=source_type(record.content_type),
    )
    db.add(document)
    await db.commit()
    return document


async def chunk_with_document(
    db: AsyncSession, kb_id: uuid.UUID, chunk_id: uuid.UUID
) -> tuple[KnowledgeChunk, KnowledgeDocument] | None:
    row = (await db.execute(
        select(KnowledgeChunk, KnowledgeDocument)
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .where(KnowledgeChunk.id == chunk_id, KnowledgeChunk.knowledge_base_id == kb_id)
    )).first()
    return (row[0], row[1]) if row else None


# --- Ingestion (the worker task) -------------------------------------------------------------


async def run_ingestion(session_factory: SessionFactory, document_id: uuid.UUID) -> dict[str, Any]:
    """Read, chunk, and embed a pending document with its owner's credentials. Safe to run
    twice: a document that isn't pending (or failed, for a retry) is left alone."""
    from app.services.credentials import build_execution_services
    from app.services.files import DbFileStore

    async with session_factory() as db:
        document = await db.get(KnowledgeDocument, document_id)
        if document is None:
            return {"document_id": str(document_id), "status": "missing"}
        # "processing" for that long means the worker died mid-way: take it over.
        stale = document.status == DocumentStatus.PROCESSING and document.updated_at < utcnow() - STALE_PROCESSING
        if document.status not in (DocumentStatus.PENDING, DocumentStatus.FAILED) and not stale:
            return {"document_id": str(document_id), "status": document.status.value, "skipped": True}
        kb = await db.get(KnowledgeBase, document.knowledge_base_id)
        owner = await db.get(User, kb.owner_id) if kb else None
        if kb is None or owner is None:
            return {"document_id": str(document_id), "status": "missing"}
        document.status, document.error = DocumentStatus.PROCESSING, None
        await db.commit()
        store = DbKnowledgeStore(db, owner.id)
        try:
            if document.file_id is None:
                raise FileNotAvailable("The uploaded file was deleted before it could be read")
            stored = await DbFileStore(db, owner.id).get(str(document.file_id))
            services = await build_execution_services(db, owner)
            result = await ingest_document(services, store, kb_info(kb), stored, str(document.id))
        except FileNotAvailable as exc:
            await store.fail_document(str(document.id), str(exc))
            return {"document_id": str(document_id), "status": "failed", "error": str(exc)}
        except KnowledgeError as exc:
            logger.warning("document ingestion failed", extra={"document_id": str(document_id), "error": str(exc)})
            return {"document_id": str(document_id), "status": "failed", "error": str(exc)}
        logger.info("document ingested", extra={"document_id": str(document_id), "chunks": result.chunk_count})
        return {"document_id": str(document_id), "status": "ready", "chunks": result.chunk_count}
