"""Secure upload validation and filename handling (PDF + DOCX)."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from app.config import Settings

PDF_MAGIC = b"%PDF"
DOCX_MAGIC = b"PK"  # ZIP / OOXML
_UNSAFE = re.compile(r"[^A-Za-z0-9._\-]+")

ALLOWED_EXTENSIONS = (".pdf", ".docx")
ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/x-pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/octet-stream",
    "binary/octet-stream",
}


def safe_store_filename(original_name: str) -> str:
    """Generate a server-side filename; never trust client paths."""
    stem = Path(original_name or "document").name
    stem = _UNSAFE.sub("_", stem)[:80] or "document"
    lower = stem.lower()
    if not lower.endswith(ALLOWED_EXTENSIONS):
        # Prefer extension from original when known; default to .pdf
        orig_ext = Path(original_name or "").suffix.lower()
        stem = f"{stem}{orig_ext if orig_ext in ALLOWED_EXTENSIONS else '.pdf'}"
    return f"{uuid.uuid4().hex}_{stem}"


def resolve_under(root: Path, filename: str) -> Path:
    """Resolve path and ensure it stays under root (no traversal)."""
    root_resolved = root.resolve()
    name = Path(filename).name
    candidate = (root_resolved / name).resolve()
    if not name or name in {".", ".."} or candidate.parent != root_resolved:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file path",
        )
    return candidate


def _detect_kind(name: str, content_type: str, data: bytes) -> str:
    lower = name.lower()
    ct = (content_type or "").lower()
    if lower.endswith(".docx") or "wordprocessingml" in ct:
        if data[:2] != DOCX_MAGIC:
            raise HTTPException(status_code=400, detail="File is not a valid DOCX (ZIP) archive")
        return "docx"
    if lower.endswith(".pdf") or ct in ("application/pdf", "application/x-pdf"):
        if PDF_MAGIC not in data[:1024]:
            raise HTTPException(status_code=400, detail="File is not a valid PDF")
        return "pdf"
    # Content-type only / octet-stream: sniff magic
    if PDF_MAGIC in data[:1024]:
        return "pdf"
    if data[:2] == DOCX_MAGIC and b"word/" in data[:8192]:
        return "docx"
    raise HTTPException(
        status_code=400,
        detail="Only PDF and DOCX interrogation report files are accepted",
    )


async def validate_pdf_upload(file: UploadFile, settings: Settings) -> bytes:
    """Validate upload; accepts PDF or DOCX. Name kept for API compatibility."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    name = file.filename.lower()
    content_type = (file.content_type or "").lower()
    if not name.endswith(ALLOWED_EXTENSIONS) and content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Only PDF and DOCX files are accepted",
        )

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
    _detect_kind(file.filename or "", content_type, data)
    return data
