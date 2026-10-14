"""Secure upload validation and filename handling."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Tuple

from fastapi import HTTPException, UploadFile, status

from app.config import Settings
from app.services.source_prep import ACCEPTED_DESCRIPTION, detect_kind

_UNSAFE = re.compile(r"[^A-Za-z0-9._\-]+")


def safe_store_filename(original_name: str, ext: str = ".pdf") -> str:
    """Generate a server-side filename; never trust client paths.

    ``ext`` is the extension of the detected file type, so the stored name always
    matches the content whatever the client called the file.
    """
    stem = Path(original_name or "document").name
    stem = _UNSAFE.sub("_", stem)[:80] or "document"
    if not stem.lower().endswith(ext):
        stem = f"{stem}{ext}"
    return f"{uuid.uuid4().hex}_{stem}"


def resolve_under(root: Path, filename: str) -> Path:
    """Resolve path and ensure it stays under root (no traversal)."""
    root_resolved = root.resolve()
    name = Path(filename).name
    candidate = (root_resolved / name).resolve()
    # parent check, not a string prefix ('uploads2' would pass startswith('uploads'))
    if not name or name in {".", ".."} or candidate.parent != root_resolved:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file path",
        )
    return candidate


async def validate_upload(file: UploadFile, settings: Settings) -> Tuple[bytes, str]:
    """Read and check an upload; returns (content, kind) with kind from ``source_prep.KINDS``.

    The type is decided from the file's content, never from its name or declared type.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    # Read in chunks so an oversized upload is rejected without buffering it all
    chunks = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds maximum size of {settings.max_upload_size_mb} MB",
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    kind = detect_kind(data, file.filename)
    if kind is None and (data.startswith(b"\x00\x05\x16\x07") or data.startswith(b"\x00\x05\x16\x00")):
        raise HTTPException(
            status_code=400,
            detail="This is a macOS metadata file (a '._' shadow copy, a few hundred bytes), not the document. "
                   "Upload the real file with the same name without the '._' prefix.",
        )
    if kind is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported or invalid file: accepted types are {ACCEPTED_DESCRIPTION}",
        )
    return data, kind
