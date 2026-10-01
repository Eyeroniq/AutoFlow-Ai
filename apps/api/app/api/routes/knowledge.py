import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from flowforge_engine import ExecutionServices
from flowforge_engine.errors import ProviderError
from flowforge_engine.knowledge import KNOWLEDGE_TYPES, retrieve

from app.api.deps import CurrentUser, DbSession
from app.api.routes.files import UPLOAD_BODY, receive_upload
from app.models.enums import DocumentStatus
from app.models.file import UploadedFile
from app.models.knowledge import KnowledgeBase, KnowledgeDocument
from app.schemas.knowledge import (
    ChunkRead,
    DocumentAdd,
    DocumentRead,
    KnowledgeBaseCreate,
    KnowledgeBaseRead,
    KnowledgeBaseUpdate,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from app.services.files import delete_file, owned_file
from app.services.knowledge import (
    DbKnowledgeStore,
    KnowledgeConflict,
    add_pending_document,
    chunk_with_document,
    create_kb,
    kb_counts,
    kb_info,
    list_documents,
    list_kbs,
    owned_kb,
    rename_kb,
)
from app.services.providers import get_execution_services
from app.services.task_queue import EnqueueFailed, IngestQueue, get_ingest_queue

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge bases"])

Services = Annotated[ExecutionServices, Depends(get_execution_services)]
IngestQueueDep = Annotated[IngestQueue, Depends(get_ingest_queue)]
_NOT_FOUND = {404: {"description": "No such knowledge base, or it isn't yours"}}


async def _owned(db: DbSession, user: CurrentUser, kb_id: uuid.UUID) -> KnowledgeBase:
    kb = await owned_kb(db, user.id, kb_id)
    if kb is None or kb.id != kb_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Knowledge base not found")
    return kb


async def _read(db: DbSession, kb: KnowledgeBase) -> KnowledgeBaseRead:
    documents, chunks = await kb_counts(db, kb.id)
    return KnowledgeBaseRead.model_validate(kb).model_copy(update={"document_count": documents, "chunk_count": chunks})


@router.get("", response_model=list[KnowledgeBaseRead], summary="Your knowledge bases (newest first)")
async def list_all(db: DbSession, user: CurrentUser) -> list[KnowledgeBaseRead]:
    return [
        KnowledgeBaseRead.model_validate(kb).model_copy(update={"document_count": d, "chunk_count": c})
        for kb, d, c in await list_kbs(db, user.id)
    ]


@router.post(
    "", response_model=KnowledgeBaseRead, status_code=status.HTTP_201_CREATED, summary="Create a knowledge base",
    responses={409: {"description": "You already have one with this name"},
               422: {"description": "Invalid settings, or the embedding provider isn't connected"}},
)
async def create(body: KnowledgeBaseCreate, db: DbSession, user: CurrentUser, services: Services) -> KnowledgeBaseRead:
    provider = body.embedding_provider
    if provider != "mock" and not services.has_credentials(provider):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=services.missing_credentials_message(provider))
    model = body.embedding_model or (None if provider == "mock" else services.settings.embedding_model(provider))
    try:
        kb = await create_kb(
            db, user.id, name=body.name, description=body.description, embedding_provider=provider,
            embedding_model=model, chunk_size=body.chunk_size, chunk_overlap=body.chunk_overlap,
        )
    except KnowledgeConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return await _read(db, kb)


@router.get("/{kb_id}", response_model=KnowledgeBaseRead, summary="One knowledge base", responses=_NOT_FOUND)
async def get_one(kb_id: uuid.UUID, db: DbSession, user: CurrentUser) -> KnowledgeBaseRead:
    return await _read(db, await _owned(db, user, kb_id))


@router.patch(
    "/{kb_id}", response_model=KnowledgeBaseRead, summary="Rename or redescribe a knowledge base",
    description="The embedding model and chunking can't change: the stored vectors were made with them.",
    responses={**_NOT_FOUND, 409: {"description": "You already have one with this name"}},
)
async def update(kb_id: uuid.UUID, body: KnowledgeBaseUpdate, db: DbSession, user: CurrentUser) -> KnowledgeBaseRead:
    kb = await _owned(db, user, kb_id)
    try:
        kb = await rename_kb(db, kb, name=body.name.strip() if body.name else None, description=body.description)
    except KnowledgeConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return await _read(db, kb)


@router.delete(
    "/{kb_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_NOT_FOUND,
    summary="Delete a knowledge base with its documents and chunks (the uploads stay under Files)",
)
async def delete(kb_id: uuid.UUID, db: DbSession, user: CurrentUser) -> Response:
    await db.delete(await _owned(db, user, kb_id))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Documents ------------------------------------------------------------------------------


@router.get("/{kb_id}/documents", response_model=list[DocumentRead], summary="Documents and their status", responses=_NOT_FOUND)
async def documents(kb_id: uuid.UUID, db: DbSession, user: CurrentUser) -> list[DocumentRead]:
    kb = await _owned(db, user, kb_id)
    return [DocumentRead.model_validate(d) for d in await list_documents(db, kb.id)]


async def _queue_document(
    db: DbSession, kb: KnowledgeBase, record: UploadedFile, ingest: IngestQueue
) -> DocumentRead:
    if record.content_type not in KNOWLEDGE_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"'{record.filename}' is {record.content_type}; knowledge bases take PDFs, images, and plain text",
        )
    document = await add_pending_document(db, kb, record)
    try:
        await ingest.enqueue_ingest(document.id)
    except EnqueueFailed as exc:
        document.status, document.error = DocumentStatus.FAILED, f"Couldn't queue the document: {exc}"
        await db.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="The task queue is unavailable; try again") from None
    return DocumentRead.model_validate(document)


@router.post(
    "/{kb_id}/documents", response_model=DocumentRead, status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document (multipart field `file`): it's read, chunked, and embedded in the background",
    description="Poll GET /documents for its status: pending → processing → ready (or failed, with the reason).",
    responses={**_NOT_FOUND, 415: {"description": "Not a PDF, image, or plain-text file"}, 503: {"description": "Queue unavailable"}},
    openapi_extra=UPLOAD_BODY,
)
async def upload_document(
    kb_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser, ingest: IngestQueueDep
) -> DocumentRead:
    kb = await _owned(db, user, kb_id)
    record = await receive_upload(request, db, user)
    if record.content_type not in KNOWLEDGE_TYPES:  # not useful under Files either
        await delete_file(db, record)
    return await _queue_document(db, kb, record, ingest)


@router.post(
    "/{kb_id}/documents/from-file", response_model=DocumentRead, status_code=status.HTTP_202_ACCEPTED,
    summary="Add a file you already uploaded (POST /api/files)",
    responses={**_NOT_FOUND, 415: {"description": "Not a PDF, image, or plain-text file"}},
)
async def add_uploaded(
    kb_id: uuid.UUID, body: DocumentAdd, db: DbSession, user: CurrentUser, ingest: IngestQueueDep
) -> DocumentRead:
    kb = await _owned(db, user, kb_id)
    record = await owned_file(db, user.id, body.file_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="File not found")
    return await _queue_document(db, kb, record, ingest)


async def _owned_document(db: DbSession, kb: KnowledgeBase, document_id: uuid.UUID) -> KnowledgeDocument:
    document = await db.get(KnowledgeDocument, document_id)
    if document is None or document.knowledge_base_id != kb.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


@router.post(
    "/{kb_id}/documents/{document_id}/retry", response_model=DocumentRead, status_code=status.HTTP_202_ACCEPTED,
    summary="Process a failed document again", responses={**_NOT_FOUND, 409: {"description": "It didn't fail"}},
)
async def retry(
    kb_id: uuid.UUID, document_id: uuid.UUID, db: DbSession, user: CurrentUser, ingest: IngestQueueDep
) -> DocumentRead:
    document = await _owned_document(db, await _owned(db, user, kb_id), document_id)
    if document.status != DocumentStatus.FAILED:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"The document is {document.status.value}, not failed")
    document.status, document.error = DocumentStatus.PENDING, None
    await db.commit()
    try:
        await ingest.enqueue_ingest(document.id)
    except EnqueueFailed:
        document.status, document.error = DocumentStatus.FAILED, "Couldn't queue the document"
        await db.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="The task queue is unavailable; try again") from None
    await db.refresh(document)
    return DocumentRead.model_validate(document)


@router.delete(
    "/{kb_id}/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_NOT_FOUND,
    summary="Remove a document and its chunks from the knowledge base",
)
async def delete_document(kb_id: uuid.UUID, document_id: uuid.UUID, db: DbSession, user: CurrentUser) -> Response:
    document = await _owned_document(db, await _owned(db, user, kb_id), document_id)
    await db.delete(document)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Search ---------------------------------------------------------------------------------


@router.post(
    "/{kb_id}/search", response_model=SearchResponse, summary="Test a search: the chunks closest to a query",
    responses={**_NOT_FOUND, 502: {"description": "The embedding provider failed"}},
)
async def search(
    kb_id: uuid.UUID, body: SearchRequest, db: DbSession, user: CurrentUser, services: Services
) -> SearchResponse:
    kb = await _owned(db, user, kb_id)
    try:
        hits = await retrieve(services, DbKnowledgeStore(db, user.id), kb_info(kb), body.query, body.top_k)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from None
    return SearchResponse(
        query=body.query, results=[SearchHit(rank=n, **h.as_dict()) for n, h in enumerate(hits, start=1)]
    )


@router.get(
    "/{kb_id}/chunks/{chunk_id}", response_model=ChunkRead, responses=_NOT_FOUND,
    summary="One chunk and where it came from (for following a citation)",
)
async def chunk(kb_id: uuid.UUID, chunk_id: uuid.UUID, db: DbSession, user: CurrentUser) -> ChunkRead:
    kb = await _owned(db, user, kb_id)
    found = await chunk_with_document(db, kb.id, chunk_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Chunk not found")
    row, document = found
    meta = row.metadata_json or {}
    return ChunkRead(
        id=row.id, document_id=document.id, filename=document.filename, chunk_index=row.chunk_index,
        page=meta.get("page"), start=meta.get("start"), end=meta.get("end"), content=row.content,
    )
