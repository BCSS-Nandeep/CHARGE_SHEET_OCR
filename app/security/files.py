"""Secure upload validation and filename handling."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from app.config import Settings

PDF_MAGIC = b"%PDF"
_UNSAFE = re.compile(r"[^A-Za-z0-9._\-]+")


def safe_store_filename(original_name: str) -> str:
    """Generate a server-side filename; never trust client paths."""
    stem = Path(original_name or "document").name
    stem = _UNSAFE.sub("_", stem)[:80] or "document"
    if not stem.lower().endswith(".pdf"):
        stem = f"{stem}.pdf"
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


async def validate_pdf_upload(file: UploadFile, settings: Settings) -> bytes:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    name = file.filename.lower()
    content_type = (file.content_type or "").lower()
    if not name.endswith(".pdf") and content_type not in (
        "application/pdf",
        "application/x-pdf",
        "binary/octet-stream",
        "application/octet-stream",
    ):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

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
    # PDF spec allows the header within the first 1024 bytes
    if PDF_MAGIC not in data[:1024]:
        raise HTTPException(status_code=400, detail="File is not a valid PDF")
    return data
