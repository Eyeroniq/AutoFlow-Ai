"""Uploaded files as nodes see them.

The engine never touches a database: the API gives each run a FileStore scoped to the
run's owner (backed by the upload volume shared by the API and every worker), and nodes
resolve file references through it. A reference is a file id, or the object an Input node
of type "file" outputs ({"file_id": ..., "filename": ..., ...}), so `{{input.document}}`
works wherever a file is expected.
"""

from __future__ import annotations

import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from flowforge_engine.errors import EngineError

PDF = "application/pdf"
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/tiff", "image/webp", "image/bmp", "image/gif"})


class FileNotAvailable(EngineError):
    """The file doesn't exist, isn't the run owner's, or its bytes are missing."""


@dataclass(frozen=True)
class StoredFile:
    id: str
    filename: str
    content_type: str
    size_bytes: int
    # Readable by this process (the shared upload volume, or a temp dir in tests).
    path: Path

    def describe(self) -> dict[str, Any]:
        """What an Input node of type "file" outputs (never the path)."""
        return {
            "file_id": self.id,
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
        }


@runtime_checkable
class FileStore(Protocol):
    async def get(self, file_id: str) -> StoredFile:
        """Raises FileNotAvailable if the file doesn't exist or isn't accessible."""
        ...


def file_id_from(value: Any) -> str:
    """The file id in a reference: an id string, or an object with "file_id"."""
    if isinstance(value, dict):
        value = value.get("file_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected a file id or a file object from an Input node of type file")
    return value.strip()


class LocalFileStore:
    """Files on local disk, registered in memory. For tests and standalone engine use."""

    def __init__(self) -> None:
        self._files: dict[str, StoredFile] = {}

    def add(self, path: str | Path, *, content_type: str | None = None, filename: str | None = None) -> StoredFile:
        path = Path(path)
        guessed = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        stored = StoredFile(
            id=str(uuid.uuid4()),
            filename=filename or path.name,
            content_type=guessed,
            size_bytes=path.stat().st_size,
            path=path,
        )
        self._files[stored.id] = stored
        return stored

    async def get(self, file_id: str) -> StoredFile:
        stored = self._files.get(file_id)
        if stored is None or not stored.path.is_file():
            raise FileNotAvailable(f"File '{file_id}' was not found")
        return stored
