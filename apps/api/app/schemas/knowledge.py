import uuid
from datetime import datetime
from typing import Literal

from flowforge_engine.knowledge import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import DocumentStatus

EmbeddingProvider = Literal["gemini", "openai", "ollama", "mock"]


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[^{}]+$", description="Nodes refer to it by this name.")
    description: str = Field(default="", max_length=2000)
    embedding_provider: EmbeddingProvider = "gemini"
    embedding_model: str | None = Field(default=None, max_length=200, description="Blank: the provider's embedding model.")
    chunk_size: int = Field(default=DEFAULT_CHUNK_SIZE, ge=200, le=8000)
    chunk_overlap: int = Field(default=DEFAULT_CHUNK_OVERLAP, ge=0, le=2000)

    @model_validator(mode="after")
    def _overlap_fits(self) -> "KnowledgeBaseCreate":
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("name can't be blank")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        return self


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[^{}]+$")
    description: str | None = Field(default=None, max_length=2000)


class KnowledgeBaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    embedding_provider: str
    embedding_model: str | None
    dimensions: int
    chunk_size: int
    chunk_overlap: int
    document_count: int = 0
    chunk_count: int = 0
    created_at: datetime
    updated_at: datetime


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    file_id: uuid.UUID | None
    filename: str
    status: DocumentStatus
    source_type: str
    method: str | None
    chunk_count: int
    char_count: int
    error: str | None
    created_at: datetime
    updated_at: datetime


class DocumentAdd(BaseModel):
    """Add an already-uploaded file (POST /api/files) instead of uploading in this request."""

    file_id: uuid.UUID


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=50)


class SearchHit(BaseModel):
    rank: int
    chunk_id: str
    document_id: str
    filename: str
    chunk_index: int
    page: int | None
    score: float
    content: str


class SearchResponse(BaseModel):
    query: str
    results: list[SearchHit]


class ChunkRead(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    page: int | None
    start: int | None
    end: int | None
    content: str
