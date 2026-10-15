"""Document metadata, listing, source PDF, reprocess, delete, templates."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.logging_utils import audit
from app.repositories.document_repository import DocumentRepository
from app.schemas.api_responses import (
    DocumentListResponse,
    DocumentStatusResponse,
    TemplateListResponse,
    TemplateProfile,
)
from app.schemas.document import DocumentStatus, ProcessingProgress
from app.security.auth import Principal, require_api_key, require_role
from app.security.files import resolve_under
from app.services.pdf_service import PdfService
from app.services.processing_service import enqueue_processing, get_progress, is_inflight
from app.services.retention_service import delete_document_content
from app.services.source_prep import KINDS, kind_of_path, prepare_source
from app.services.template_service import TemplateService

router = APIRouter(prefix="/api/v1", tags=["documents"])


def _get_live(repo: DocumentRepository, document_id: str):
    doc = repo.get(document_id)
    if not doc or doc.status == DocumentStatus.DELETED.value:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


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
    meta = repo.to_meta(_get_live(repo, document_id))
    progress = get_progress(document_id)
    if progress:
        meta.progress = ProcessingProgress(**progress)
    return DocumentStatusResponse(document=meta)


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
    doc = _get_live(repo, document_id)
    path = resolve_under(settings.upload_dir, doc.stored_filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Source document not available")
    kind = kind_of_path(path)
    return FileResponse(
        path,
        media_type=KINDS[kind].media_type,
        filename=doc.original_filename,
        # a DOCX cannot be shown inline by a browser
        content_disposition_type="attachment" if kind == "docx" else "inline",
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/documents/{document_id}/pages/{page}/image",
    dependencies=[Depends(require_api_key)],
)
def get_page_image(
    document_id: str,
    page: int,
    width: int = Query(900, ge=200, le=2400),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """PNG of one page for the side-by-side preview (rendered from the stored document;
    for an image / DOCX upload, from the PDF derived from it). The original is untouched."""
    repo = DocumentRepository(db)
    doc = _get_live(repo, document_id)
    path = resolve_under(settings.upload_dir, doc.stored_filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Source document not available")
    try:
        work = prepare_source(path)
        png = PdfService().render_page_image(work, page, max_side=width * 2)
    except ValueError:
        raise HTTPException(status_code=404, detail="Page not found")
    except Exception:
        raise HTTPException(status_code=422, detail="This document cannot be shown as page images")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post(
    "/documents/{document_id}/reprocess",
    response_model=DocumentStatusResponse,
)
def reprocess_document(
    document_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_role("operator")),
) -> DocumentStatusResponse:
    repo = DocumentRepository(db)
    doc = _get_live(repo, document_id)
    if doc.status in (DocumentStatus.PROCESSING.value, DocumentStatus.QUEUED.value) and is_inflight(document_id):
        raise HTTPException(status_code=409, detail="Document is already being processed")
    # A new extraction invalidates any previous verification
    repo.update_status(document_id, DocumentStatus.QUEUED, error_message="", verified=False, requires_review=True)
    audit("reprocess_requested", document_id=document_id, user=principal.name)
    enqueue_processing(document_id)
    return DocumentStatusResponse(document=repo.to_meta(repo.get(document_id)))


@router.delete(
    "/documents/{document_id}",
    status_code=204,
    response_class=Response,
)
def delete_document(
    document_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(require_role("admin")),
) -> Response:
    """Permanently delete the PDF, extracted results and corrections (tombstone kept)."""
    repo = DocumentRepository(db)
    doc = _get_live(repo, document_id)
    if is_inflight(document_id):
        raise HTTPException(status_code=409, detail="Document is being processed; try again when it finishes")
    delete_document_content(repo, doc, settings.upload_dir)
    db.commit()
    audit("document_deleted", document_id=document_id, user=principal.name)
    return Response(status_code=204)


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
