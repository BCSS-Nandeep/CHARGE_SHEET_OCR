"""PDF upload endpoint."""

from __future__ import annotations

from pathlib import Path

import aiofiles
from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.logging_utils import audit
from app.repositories.document_repository import DocumentRepository
from app.schemas.api_responses import ProcessingAcceptedResponse
from app.schemas.document import DocumentStatus
from app.security.auth import Principal, require_role
from app.security.files import resolve_under, safe_store_filename, validate_upload
from app.services.processing_service import enqueue_processing
from app.services.source_prep import KINDS

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


@router.post(
    "/upload",
    response_model=ProcessingAcceptedResponse,
    status_code=202,
)
async def upload_document(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(require_role("operator")),
) -> ProcessingAcceptedResponse:
    data, kind = await validate_upload(file, settings)
    stored = safe_store_filename(file.filename or "document", KINDS[kind].ext)
    dest: Path = resolve_under(settings.upload_dir, stored)

    async with aiofiles.open(dest, "wb") as out:  # the original, byte for byte
        await out.write(data)

    repo = DocumentRepository(db)
    doc = repo.create(
        original_filename=Path(file.filename or "document").name,
        stored_filename=stored,
        content_type=KINDS[kind].media_type,
        size_bytes=len(data),
    )
    repo.update_status(doc.id, DocumentStatus.QUEUED)
    audit("upload_accepted", document_id=doc.id, size_bytes=len(data), kind=kind, user=principal.name)
    enqueue_processing(doc.id)

    return ProcessingAcceptedResponse(
        document_id=doc.id,
        status=DocumentStatus.QUEUED,
        message="Document accepted for processing",
        poll_url=f"/api/v1/documents/{doc.id}",
    )
