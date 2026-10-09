"""Document metadata, listing, source PDF, reprocess, templates."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.repositories.document_repository import DocumentRepository
from app.schemas.api_responses import (
    DocumentListResponse,
    DocumentStatusResponse,
    TemplateListResponse,
    TemplateProfile,
)
from app.schemas.document import DocumentStatus
from app.security.auth import require_api_key
from app.security.files import resolve_under
from app.services.processing_service import enqueue_processing
from app.services.template_service import TemplateService

router = APIRouter(prefix="/api/v1", tags=["documents"])


@router.get(
    "/documents",
    response_model=DocumentListResponse,
    dependencies=[Depends(require_api_key)],
)
def list_documents(
    status: Optional[DocumentStatus] = None,
    verified: Optional[bool] = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> DocumentListResponse:
    repo = DocumentRepository(db)
    total, rows = repo.list(status=status, verified=verified, limit=limit, offset=offset)
    return DocumentListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[repo.to_meta(r) for r in rows],
    )


@router.get(
    "/documents/{document_id}",
    response_model=DocumentStatusResponse,
    dependencies=[Depends(require_api_key)],
)
def get_document(document_id: str, db: Session = Depends(get_db)) -> DocumentStatusResponse:
    repo = DocumentRepository(db)
    doc = repo.get(document_id)
    if not doc or doc.status == DocumentStatus.DELETED.value:
        raise HTTPException(status_code=404, detail="Document not found")
    return DocumentStatusResponse(document=repo.to_meta(doc))


@router.get(
    "/documents/{document_id}/source",
    dependencies=[Depends(require_api_key)],
)
def get_source_pdf(
    document_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    repo = DocumentRepository(db)
    doc = repo.get(document_id)
    if not doc or doc.status == DocumentStatus.DELETED.value:
        raise HTTPException(status_code=404, detail="Document not found")
    path = resolve_under(settings.upload_dir, doc.stored_filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Source PDF not available")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=doc.original_filename,
        content_disposition_type="inline",
    )


@router.post(
    "/documents/{document_id}/reprocess",
    response_model=DocumentStatusResponse,
    dependencies=[Depends(require_api_key)],
)
def reprocess_document(document_id: str, db: Session = Depends(get_db)) -> DocumentStatusResponse:
    repo = DocumentRepository(db)
    doc = repo.get(document_id)
    if not doc or doc.status == DocumentStatus.DELETED.value:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.status == DocumentStatus.PROCESSING.value:
        raise HTTPException(status_code=409, detail="Document is already processing")
    repo.update_status(document_id, DocumentStatus.QUEUED, error_message="")
    enqueue_processing(document_id)
    doc = repo.get(document_id)
    return DocumentStatusResponse(document=repo.to_meta(doc))


@router.get(
    "/templates",
    response_model=TemplateListResponse,
    dependencies=[Depends(require_api_key)],
)
def list_templates(db: Session = Depends(get_db)) -> TemplateListResponse:
    rows = TemplateService(db).list_profiles()
    items = [
        TemplateProfile(
            id=r.id,
            fingerprint=r.fingerprint,
            label=r.label,
            document_count=r.document_count or 0,
            verified=bool(r.verified),
            created_at=r.created_at,
            last_seen_at=r.last_seen_at,
        )
        for r in rows
    ]
    return TemplateListResponse(items=items)
