import uuid
from typing import Any

from flowforge_engine.knowledge import EMBEDDING_DIMENSIONS
from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import DocumentStatus, pg_enum


class KnowledgeBase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A user's collection of documents, searched by meaning (Retriever node, test search).

    Every chunk is embedded by `embedding_provider`/`embedding_model` into
    EMBEDDING_DIMENSIONS numbers; queries must use the same model, so these can't change
    once documents are in (the API refuses).
    """

    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Nodes refer to a knowledge base by this name (or its id).
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    embedding_provider: Mapped[str] = mapped_column(String(50))
    # None: the provider's default embedding model at the time it was created, recorded here.
    embedding_model: Mapped[str | None] = mapped_column(String(200))
    dimensions: Mapped[int] = mapped_column(Integer, default=EMBEDDING_DIMENSIONS, server_default=str(EMBEDDING_DIMENSIONS))
    chunk_size: Mapped[int] = mapped_column(Integer)
    chunk_overlap: Mapped[int] = mapped_column(Integer)


class KnowledgeDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One file added to a knowledge base, and how far its processing got."""

    __tablename__ = "kb_documents"

    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    # The upload it was read from; kept as metadata if the file is deleted later.
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id", ondelete="SET NULL"))
    filename: Mapped[str] = mapped_column(String(255))
    status: Mapped[DocumentStatus] = mapped_column(
        pg_enum(DocumentStatus, "document_status"), default=DocumentStatus.PENDING, server_default="pending"
    )
    # "pdf", "image", or "text": what was uploaded.
    source_type: Mapped[str] = mapped_column(String(20))
    # How the text was read: "text", "text_layer", "ocr", or "mixed" (set when ready).
    method: Mapped[str | None] = mapped_column(String(20))
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    char_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)


class KnowledgeChunk(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A piece of a document's text and its embedding."""

    __tablename__ = "kb_chunks"
    __table_args__ = (
        # Approximate nearest-neighbour search by cosine distance (the <=> operator).
        Index(
            "ix_kb_chunks_embedding_hnsw", "embedding",
            postgresql_using="hnsw", postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_documents.id", ondelete="CASCADE"), index=True)
    # Denormalized from the document, so a search filters chunks without a join.
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    # {"page": 3, "start": 120, "end": 1080}: where the chunk came from, for citations.
    metadata_json: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
