"""Extraction results, corrections, and verification."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.repositories.document_repository import DocumentRepository
from app.repositories.results_repository import ResultsRepository
from app.schemas.api_responses import (
    FieldCorrectionRequest,
    ResultsResponse,
    VerifyRequest,
)
from app.schemas.document import DocumentStatus
from app.security.auth import require_api_key

router = APIRouter(prefix="/api/v1/documents", tags=["results"])


@router.get(
    "/{document_id}/results",
    response_model=ResultsResponse,
    dependencies=[Depends(require_api_key)],
)
def get_results(document_id: str, db: Session = Depends(get_db)) -> ResultsResponse:
    repo = DocumentRepository(db)
    doc = repo.get(document_id)
    if not doc or doc.status == DocumentStatus.DELETED.value:
        raise HTTPException(status_code=404, detail="Document not found")
    results = ResultsRepository(db).get(document_id)
    corrections = repo.get_corrections(document_id)
    if doc.status in (
        DocumentStatus.UPLOADED.value,
        DocumentStatus.QUEUED.value,
        DocumentStatus.PROCESSING.value,
    ):
        return ResultsResponse(
            document=repo.to_meta(doc),
            results=results,
            field_corrections=corrections,
        )
    if doc.status == DocumentStatus.FAILED.value:
        raise HTTPException(
            status_code=422,
            detail=doc.error_message or "Processing failed",
        )
    if results is None:
        raise HTTPException(status_code=404, detail="Results not available")
    return ResultsResponse(
        document=repo.to_meta(doc),
        results=results,
        field_corrections=corrections,
    )


@router.patch(
    "/{document_id}/fields",
    response_model=ResultsResponse,
    dependencies=[Depends(require_api_key)],
)
def patch_fields(
    document_id: str,
    body: FieldCorrectionRequest,
    db: Session = Depends(get_db),
) -> ResultsResponse:
    repo = DocumentRepository(db)
    doc = repo.get(document_id)
    if not doc or doc.status == DocumentStatus.DELETED.value:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.status not in (
        DocumentStatus.COMPLETED.value,
        DocumentStatus.VERIFIED.value,
    ):
        raise HTTPException(status_code=409, detail="Results not ready for correction")

    corr_payload = [
        {
            "canonical_name": c.canonical_name,
            "corrected_value": c.corrected_value,
            "review_status": c.review_status.value,
            "notes": c.notes,
        }
        for c in body.corrections
    ]
    repo.add_corrections(document_id, corr_payload)
    # Apply without overwriting original_value
    applied = {
        c["canonical_name"]: {
            "corrected_value": c["corrected_value"],
            "review_status": c["review_status"],
            "notes": c.get("notes"),
        }
        for c in corr_payload
    }
    ResultsRepository(db).apply_corrections(document_id, applied)
    results = ResultsRepository(db).get(document_id)
    return ResultsResponse(
        document=repo.to_meta(doc),
        results=results,
        field_corrections=repo.get_corrections(document_id),
    )


@router.post(
    "/{document_id}/verify",
    response_model=ResultsResponse,
    dependencies=[Depends(require_api_key)],
)
def verify_document(
    document_id: str,
    body: VerifyRequest,
    db: Session = Depends(get_db),
) -> ResultsResponse:
    repo = DocumentRepository(db)
    doc = repo.get(document_id)
    if not doc or doc.status == DocumentStatus.DELETED.value:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.status not in (
        DocumentStatus.COMPLETED.value,
        DocumentStatus.VERIFIED.value,
    ):
        raise HTTPException(status_code=409, detail="Document not ready for verification")
    if body.verified and not (body.notes and str(body.notes).strip()):
        raise HTTPException(
            status_code=400,
            detail="Verification notes are required before marking results verified",
        )

    # Audit trail: store verification note as a correction-like record
    if body.notes:
        repo.add_corrections(
            document_id,
            [
                {
                    "canonical_name": "_verification",
                    "corrected_value": body.notes,
                    "review_status": "verified" if body.verified else "unreviewed",
                    "notes": "human_verification",
                }
            ],
        )

    status = DocumentStatus.VERIFIED if body.verified else DocumentStatus.COMPLETED
    repo.update_status(
        document_id,
        status,
        verified=body.verified,
        requires_review=not body.verified,
    )
    doc = repo.get(document_id)
    return ResultsResponse(
        document=repo.to_meta(doc),
        results=ResultsRepository(db).get(document_id),
        field_corrections=repo.get_corrections(document_id),
    )
