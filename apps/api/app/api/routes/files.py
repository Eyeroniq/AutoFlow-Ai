import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException

from app.api.deps import CurrentUser, DbSession
from app.core.config import settings
from app.schemas.file import FileRead
from app.services.files import (
    ALLOWED_TYPES,
    UploadRejected,
    delete_file,
    file_path,
    list_files,
    owned_file,
    save_upload,
)

router = APIRouter(prefix="/files", tags=["files"])

_NOT_FOUND = {404: {"description": "No such file, or it isn't yours"}}


async def _owned(db: DbSession, user: CurrentUser, file_id: uuid.UUID):
    record = await owned_file(db, user.id, file_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="File not found")
    return record


@router.post(
    "",
    response_model=FileRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file (multipart field `file`)",
    description=(
        f"Accepted: {', '.join(ALLOWED_TYPES.values())}, detected from the file's content. Up to "
        f"MAX_UPLOAD_MB ({settings.MAX_UPLOAD_MB:g} MB here), or MAX_MEDIA_UPLOAD_MB "
        f"({settings.MAX_MEDIA_UPLOAD_MB:g} MB) for audio and video. Use the returned `id` as the value of an Input "
        "node of type `file` (in `inputs` for a run, or as its default)."
    ),
    responses={
        400: {"description": "No file, or an empty one"},
        411: {"description": "The request has no Content-Length"},
        413: {"description": "Larger than MAX_UPLOAD_MB (MAX_MEDIA_UPLOAD_MB for audio/video)"},
        415: {"description": "Not an allowed file type"},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    },
)
async def upload(request: Request, db: DbSession, user: CurrentUser) -> FileRead:
    # Check the size before the body is parsed (and spooled) at all.
    declared = request.headers.get("content-length")
    if declared is None:
        raise HTTPException(status.HTTP_411_LENGTH_REQUIRED, detail="Content-Length is required")
    try:
        declared_size = int(declared)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid Content-Length") from None
    largest = max(settings.MAX_UPLOAD_MB, settings.MAX_MEDIA_UPLOAD_MB)
    if declared_size > largest * 1024 * 1024 + 64 * 1024:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail=f"The file is larger than the {largest:g} MB limit")
    try:
        form = await request.form(max_files=1, max_fields=4)
    except MultiPartException as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Invalid upload: {exc.message}") from None
    try:
        upload_file = form.get("file")
        if not isinstance(upload_file, UploadFile):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Send the file as the multipart field 'file'")
        try:
            record = await save_upload(db, user.id, upload_file, declared_size=declared_size)
        except UploadRejected as exc:
            raise HTTPException(exc.status_code, detail=str(exc)) from None
    finally:
        await form.close()
    return FileRead.model_validate(record)


@router.get("", response_model=list[FileRead], summary="List your uploaded files (newest first)")
async def list_all(db: DbSession, user: CurrentUser) -> list[FileRead]:
    return [FileRead.model_validate(r) for r in await list_files(db, user.id)]


@router.get("/{file_id}", response_model=FileRead, summary="One file's metadata", responses=_NOT_FOUND)
async def get_one(file_id: uuid.UUID, db: DbSession, user: CurrentUser) -> FileRead:
    return FileRead.model_validate(await _owned(db, user, file_id))


@router.get(
    "/{file_id}/content",
    summary="Download a file",
    response_class=FileResponse,
    responses={**_NOT_FOUND, 200: {"description": "The file's bytes, as an attachment"}},
)
async def download(file_id: uuid.UUID, db: DbSession, user: CurrentUser) -> FileResponse:
    record = await _owned(db, user, file_id)
    path = file_path(record)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="The file's contents are missing from storage")
    # Always an attachment with the detected type, never rendered by the browser here.
    return FileResponse(
        path, media_type=record.content_type, filename=record.filename,
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a file", responses=_NOT_FOUND)
async def delete(file_id: uuid.UUID, db: DbSession, user: CurrentUser) -> Response:
    await delete_file(db, await _owned(db, user, file_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
