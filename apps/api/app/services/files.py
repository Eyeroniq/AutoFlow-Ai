"""Uploaded files: validation, storage on the shared volume, and owner-scoped access.

- **Size:** streamed to disk in chunks and aborted past MAX_UPLOAD_MB (413); an honest
  Content-Length over the limit is refused before reading.
- **Type:** decided by the file's first bytes (magic numbers), never the client's
  Content-Type or the extension: PDF, PNG, JPEG, TIFF, WebP, BMP, GIF, or UTF-8 text.
  Anything else is 415.
- **Storage:** FILES_DIR/<owner id>/<file id>. The path never contains the uploaded name,
  so a name like "../../etc/passwd" can't escape; that name is kept, sanitized, for display.
- **Access:** every read goes through the owner's id; another user's file is a 404, the
  same as a missing one.
"""

import hashlib
import logging
import os
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from flowforge_engine import FileNotAvailable, StoredFile, WorkflowGraph, file_id_from
from flowforge_engine.variables import contains_reference
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.file import UploadedFile

logger = logging.getLogger(__name__)

CHUNK_BYTES = 1024 * 1024
SNIFF_BYTES = 4096

# Detected type -> what we tell people it is.
ALLOWED_TYPES = {
    "application/pdf": "PDF",
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/tiff": "TIFF",
    "image/webp": "WebP",
    "image/bmp": "BMP",
    "image/gif": "GIF",
    "text/plain": "plain text",
}


class UploadRejected(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


def sniff_content_type(head: bytes) -> str | None:
    """The file's type from its first bytes, or None if it isn't an allowed type."""
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    # "BM", then a DIB header whose size field is one of the known header sizes.
    if head.startswith(b"BM") and int.from_bytes(head[14:18], "little") in {12, 40, 52, 56, 64, 108, 124}:
        return "image/bmp"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    # Text: valid UTF-8 with no control characters besides tab, newline, CR, and form feed
    # (binary formats such as ZIP/DOCX start with bytes like \x03\x04, which are valid UTF-8).
    if head and not any(byte < 0x20 and byte not in b"\t\n\r\f" or byte == 0x7F for byte in head):
        try:
            head.decode("utf-8")
        except UnicodeDecodeError as exc:
            # A multi-byte character cut off at the end of the sniffed chunk is fine.
            if exc.start < len(head) - 3:
                return None
        return "text/plain"
    return None


def safe_filename(name: str | None) -> str:
    """A display name without directories, control characters, or odd whitespace."""
    name = unicodedata.normalize("NFC", name or "")
    name = re.split(r"[\\/]", name)[-1]  # drop any path, Windows or POSIX
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return "upload"
    if len(name) > 200:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 200 - len(ext) - 1] + "." + ext) if dot and len(ext) <= 10 else name[:200]
    return name


def storage_root() -> Path:
    return Path(settings.FILES_DIR)


def _path(record: UploadedFile) -> Path:
    return storage_root() / record.storage_key


async def save_upload(db: AsyncSession, owner_id: uuid.UUID, upload: UploadFile, *, declared_size: int | None) -> UploadedFile:
    """Validate and store an upload. Raises UploadRejected (413/415/400)."""
    limit = settings.max_upload_bytes
    limit_text = f"{settings.MAX_UPLOAD_MB:g} MB"
    if declared_size is not None and declared_size > limit + 64 * 1024:  # multipart overhead
        raise UploadRejected(413, f"The file is larger than the {limit_text} limit")

    file_id = uuid.uuid4()
    storage_key = f"{owner_id}/{file_id}"
    target = storage_root() / storage_key
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{file_id}.part")
    digest = hashlib.sha256()
    size = 0
    content_type: str | None = None
    try:
        with partial.open("wb") as out:
            while chunk := await upload.read(CHUNK_BYTES):
                if content_type is None:
                    # The first chunk is at least SNIFF_BYTES unless the file is smaller.
                    content_type = sniff_content_type(chunk[:SNIFF_BYTES])
                    if content_type is None:
                        raise UploadRejected(
                            415, "Unsupported file type. Upload a PDF, an image (PNG, JPEG, TIFF, WebP, BMP, GIF), or plain text."
                        )
                size += len(chunk)
                if size > limit:
                    raise UploadRejected(413, f"The file is larger than the {limit_text} limit")
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise UploadRejected(400, "The file is empty")
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    record = UploadedFile(
        id=file_id,
        owner_id=owner_id,
        filename=safe_filename(upload.filename),
        content_type=content_type or "application/octet-stream",
        size_bytes=size,
        sha256=digest.hexdigest(),
        storage_key=storage_key,
    )
    db.add(record)
    try:
        await db.commit()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    logger.info("file uploaded", extra={"file_id": str(file_id), "content_type": record.content_type, "size_bytes": size})
    return record


async def owned_file(db: AsyncSession, owner_id: uuid.UUID, file_id: uuid.UUID) -> UploadedFile | None:
    return await db.scalar(select(UploadedFile).where(UploadedFile.id == file_id, UploadedFile.owner_id == owner_id))


async def list_files(db: AsyncSession, owner_id: uuid.UUID) -> list[UploadedFile]:
    return list(await db.scalars(
        select(UploadedFile).where(UploadedFile.owner_id == owner_id).order_by(UploadedFile.created_at.desc())
    ))


async def delete_file(db: AsyncSession, record: UploadedFile) -> None:
    path = _path(record)
    await db.delete(record)
    await db.commit()
    path.unlink(missing_ok=True)


def file_path(record: UploadedFile) -> Path:
    return _path(record)


class DbFileStore:
    """The engine's FileStore for one user's runs: ids resolve only to that user's files."""

    def __init__(self, db: AsyncSession, owner_id: uuid.UUID):
        self._db = db
        self._owner_id = owner_id

    async def get(self, file_id: str) -> StoredFile:
        try:
            parsed = uuid.UUID(str(file_id))
        except ValueError:
            raise FileNotAvailable(f"'{file_id}' is not a file id") from None
        record = await owned_file(self._db, self._owner_id, parsed)
        if record is None:
            raise FileNotAvailable(f"File '{file_id}' was not found (it doesn't exist or isn't yours)")
        path = _path(record)
        if not path.is_file():
            raise FileNotAvailable(f"The contents of '{record.filename}' are missing from file storage")
        return StoredFile(
            id=str(record.id),
            filename=record.filename,
            content_type=record.content_type,
            size_bytes=record.size_bytes,
            path=path,
        )


async def file_input_issues(
    db: AsyncSession, owner_id: uuid.UUID, graph: WorkflowGraph, inputs: dict[str, Any]
) -> list[dict[str, Any]]:
    """Problems with a run's file inputs, checked before anything is queued: each Input
    node of type file must reference one of the caller's own uploads (the value from
    `inputs`, else the node's default). Missing required values are left to the node."""
    issues = []
    for node in graph.nodes:
        if node.type != "input" or node.config.get("input_type") != "file":
            continue
        name = node.config.get("name") or node.id
        value = inputs.get(name, node.config.get("default"))
        if value is None or contains_reference(value):
            continue
        try:
            parsed = uuid.UUID(file_id_from(value))
        except ValueError:
            problem = "is not a file id"
        else:
            problem = None if await owned_file(db, owner_id, parsed) else "was not found (it doesn't exist or isn't yours)"
        if problem:
            issues.append({
                "code": "invalid_input",
                "message": f"Input '{name}': the file {problem}. Upload it with POST /api/files and use the returned id.",
                "node_id": node.id,
                "field": "default" if name not in inputs else None,
            })
    return issues


async def import_file(db: AsyncSession, owner_id: uuid.UUID, source: Path, filename: str | None = None) -> UploadedFile:
    """Store a file from local disk as if `owner_id` had uploaded it (the seed's sample)."""
    data = source.read_bytes()
    content_type = sniff_content_type(data[:SNIFF_BYTES])
    if content_type is None:
        raise UploadRejected(415, f"{source.name} is not an allowed file type")
    file_id = uuid.uuid4()
    record = UploadedFile(
        id=file_id, owner_id=owner_id, filename=safe_filename(filename or source.name), content_type=content_type,
        size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), storage_key=f"{owner_id}/{file_id}",
    )
    target = _path(record)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    db.add(record)
    await db.flush()
    return record
