"""Extraction results, corrections, and verification."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.logging_utils import audit
from app.repositories.document_repository import DocumentRepository
from app.repositories.results_repository import ResultsRepository
from app.schemas.api_responses import (
    FieldCorrectionRequest,
    ResultsResponse,
    VerifyRequest,
)
from app.schemas.document import DocumentStatus
from app.schemas.raw import RawResponse
from app.security.auth import Principal, require_api_key, require_role

router = APIRouter(prefix="/api/v1/documents", tags=["results"])

_READY = (DocumentStatus.COMPLETED.value, DocumentStatus.VERIFIED.value)


def _get_live(repo: DocumentRepository, document_id: str):
    doc = repo.get(document_id)
    if not doc or doc.status == DocumentStatus.DELETED.value:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.get(
    "/{document_id}/results",
    response_model=ResultsResponse,
    dependencies=[Depends(require_api_key)],
)
def get_results(document_id: str, db: Session = Depends(get_db)) -> ResultsResponse:
    repo = DocumentRepository(db)
    doc = _get_live(repo, document_id)
    results = ResultsRepository(db).get(document_id)
    corrections = repo.get_corrections(document_id)
    if doc.status in (
        DocumentStatus.UPLOADED.value,
        DocumentStatus.QUEUED.value,
        DocumentStatus.PROCESSING.value,
    ):
        # Previous results (if any) belong to an older run; don't present them as current
        return ResultsResponse(document=repo.to_meta(doc), results=None, field_corrections=corrections)
    if doc.status == DocumentStatus.FAILED.value:
        detail = {
            "message": doc.error_message or "Processing failed",
            "extraction_status": results.extraction_status.value if results else "failed",
        }
        if results is not None:
            # Diagnostics only — no extracted values are returned for a failed extraction
            detail["warnings"] = results.extraction_warnings
            detail["stage_results"] = [s.model_dump() for s in results.stage_results]
            detail["page_summaries"] = [p.model_dump() for p in results.page_summaries]
        raise HTTPException(status_code=422, detail=detail)
    if results is None:
        raise HTTPException(status_code=404, detail="Results not available")
    return ResultsResponse(
        document=repo.to_meta(doc),
        results=results,
        field_corrections=corrections,
    )


@router.get(
    "/{document_id}/raw",
    response_model=RawResponse,
    dependencies=[Depends(require_api_key)],
)
def get_raw(document_id: str, download: bool = False, db: Session = Depends(get_db)):
    """Every text line read from the PDF, page by page, exactly as read (no labels or grouping).

    ``?download=true`` returns the same JSON as a file attachment.
    """
    repo = DocumentRepository(db)
    doc = _get_live(repo, document_id)
    if doc.status in (DocumentStatus.UPLOADED.value, DocumentStatus.QUEUED.value, DocumentStatus.PROCESSING.value):
        raise HTTPException(status_code=409, detail="Document is still being processed")
    raw = ResultsRepository(db).get_raw(document_id)
    if raw is None:
        raise HTTPException(
            status_code=404,
            detail="No raw JSON stored for this document (processed by an older version) — reprocess it to generate one",
        )
    body = RawResponse(document_id=doc.id, file_name=doc.original_filename, **raw.model_dump())
    audit("raw_viewed", document_id=document_id, download=download)
    if not download:
        return body
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(doc.original_filename).stem)[:120] or "document"
    return Response(
        content=body.model_dump_json(indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{stem}_raw.json"'},
    )


@router.get(
    "/{document_id}/corrections",
    dependencies=[Depends(require_api_key)],
)
def correction_history(document_id: str, db: Session = Depends(get_db)) -> List[dict]:
    """Full audit trail of corrections and verification notes (oldest first)."""
    repo = DocumentRepository(db)
    _get_live(repo, document_id)
    return repo.correction_history(document_id)


@router.patch(
    "/{document_id}/fields",
    response_model=ResultsResponse,
)
def patch_fields(
    document_id: str,
    body: FieldCorrectionRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_role("operator")),
) -> ResultsResponse:
    repo = DocumentRepository(db)
    doc = _get_live(repo, document_id)
    if doc.status not in _READY:
        raise HTTPException(status_code=409, detail="Results not ready for correction")
    results_repo = ResultsRepository(db)
    data = results_repo.get(document_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Results not available")
    fmap = data.field_map()
    unknown = [c.field_id for c in body.corrections if c.field_id not in fmap]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown field(s): {', '.join(unknown[:20])}")

    # The correction history stores the field id in its canonical_name column
    corr_payload = [
        {
            "canonical_name": c.field_id,
            "corrected_value": c.corrected_value,
            "original_value": fmap[c.field_id].value,
            "review_status": c.review_status.value,
            "notes": c.notes,
            "reviewer": principal.name,
        }
        for c in body.corrections
    ]
    repo.add_corrections(document_id, corr_payload)
    # Applied alongside, never overwriting original_value
    results_repo.apply_corrections(document_id, {c["canonical_name"]: c for c in corr_payload})
    if doc.status == DocumentStatus.VERIFIED.value:
        # Editing verified results re-opens review
        repo.update_status(document_id, DocumentStatus.COMPLETED, verified=False, requires_review=True)
    audit("fields_corrected", document_id=document_id, count=len(corr_payload), user=principal.name)
    return ResultsResponse(
        document=repo.to_meta(repo.get(document_id)),
        results=results_repo.get(document_id),
        field_corrections=repo.get_corrections(document_id),
    )


@router.post(
    "/{document_id}/verify",
    response_model=ResultsResponse,
)
def verify_document(
    document_id: str,
    body: VerifyRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_role("reviewer")),
) -> ResultsResponse:
    repo = DocumentRepository(db)
    doc = _get_live(repo, document_id)
    if doc.status not in _READY:
        raise HTTPException(status_code=409, detail="Document not ready for verification")
    if body.verified and not (body.notes and str(body.notes).strip()):
        raise HTTPException(
            status_code=400,
            detail="Verification notes are required before marking results verified",
        )

    if body.notes:
        repo.add_corrections(
            document_id,
            [
                {
                    "canonical_name": "_verification",
                    "corrected_value": body.notes,
                    "review_status": "verified" if body.verified else "unreviewed",
                    "notes": "human_verification",
                    "reviewer": principal.name,
                }
            ],
        )

    status = DocumentStatus.VERIFIED if body.verified else DocumentStatus.COMPLETED
    repo.update_status(
        document_id,
        status,
        verified=body.verified,
        verified_by=principal.name,
        requires_review=not body.verified,
    )
    audit("verification", document_id=document_id, verified=body.verified, user=principal.name)
    doc = repo.get(document_id)
    return ResultsResponse(
        document=repo.to_meta(doc),
        results=ResultsRepository(db).get(document_id),
        field_corrections=repo.get_corrections(document_id),
    )
