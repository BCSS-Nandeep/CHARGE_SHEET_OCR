"""Standard-template IR: the fixed JSON and DOCX of an extracted report, plus the blank template."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.ir_standard.docx_writer import ReviewState, render_docx
from app.ir_standard.paths import apply_overrides
from app.logging_utils import audit
from app.repositories.document_repository import DocumentRepository
from app.repositories.results_repository import ResultsRepository
from app.schemas.charge_sheet import ReviewStatus
from app.schemas.document import DocumentStatus
from app.security.auth import require_api_key

router = APIRouter(prefix="/api/v1", tags=["standard-ir"])

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_BUSY = (DocumentStatus.UPLOADED.value, DocumentStatus.QUEUED.value, DocumentStatus.PROCESSING.value)


def _review_state(doc, data) -> ReviewState:
    """Final only when a reviewer verified the document in the workflow (extraction never does).

    Fields a reviewer corrected or confirmed are treated as resolved; anything else that was flagged
    stays marked NEEDS REVIEW in a draft.
    """
    verified = doc.status == DocumentStatus.VERIFIED.value and bool(doc.verified)
    return ReviewState(
        final=verified,
        verified_by=doc.verified_by if verified else None,
        verified_at=doc.processed_at.strftime("%Y-%m-%d %H:%M UTC") if verified and doc.processed_at else None,
        resolved={f.id for f in data.fields if f.review_status in (ReviewStatus.CORRECTED, ReviewStatus.VERIFIED)},
    )


def _standard_for(document_id: str, db: Session):
    """(document, standard JSON with reviewer corrections applied, stored results)."""
    repo = DocumentRepository(db)
    doc = repo.get(document_id)
    if not doc or doc.status == DocumentStatus.DELETED.value:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.status in _BUSY:
        raise HTTPException(status_code=409, detail="Document is still being processed")
    data = ResultsRepository(db).get(document_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Results not available")
    if not data.standard_ir:
        raise HTTPException(
            status_code=404,
            detail="This document was not mapped onto the standard template "
            "(enable IR_STANDARD_ENABLED with a local VLM and reprocess it)",
        )
    overrides: Dict[str, Optional[str]] = {
        f.id: f.corrected_value for f in data.fields if f.corrected_value is not None
    }
    return doc, apply_overrides(data.standard_ir, overrides), data


def _stem(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem)[:120] or "document"


@router.get("/documents/{document_id}/standard", dependencies=[Depends(require_api_key)])
def get_standard(document_id: str, download: bool = False, db: Session = Depends(get_db)):
    """The report mapped onto the standard 32-section template (reviewer corrections applied)."""
    doc, standard, _ = _standard_for(document_id, db)
    audit("standard_json_viewed", document_id=document_id, download=download)
    if not download:
        return standard
    return Response(
        content=json.dumps(standard, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{_stem(doc.original_filename)}_standard.json"'},
    )


@router.get("/documents/{document_id}/standard.docx", dependencies=[Depends(require_api_key)])
def get_standard_docx(document_id: str, db: Session = Depends(get_db)) -> Response:
    """The report as a DOCX in the standard template layout (selected options in bold).

    Until a reviewer verifies the document it is a DRAFT: flagged fields are marked NEEDS REVIEW (values
    unchanged) and the banner / page header say it is not final. Only a verified document is rendered as
    a final report, and editing a verified document re-opens review, which turns it back into a draft.
    """
    doc, standard, data = _standard_for(document_id, db)
    state = _review_state(doc, data)
    audit("standard_docx_downloaded", document_id=document_id, final=state.final)
    suffix = "standard" if state.final else "standard_DRAFT_needs_review"
    return Response(
        content=render_docx(standard, review=state),
        media_type=_DOCX,
        headers={"Content-Disposition": f'attachment; filename="{_stem(doc.original_filename)}_{suffix}.docx"',
                 "X-Report-Status": "verified-final" if state.final else "draft-needs-review"},
    )


@router.get("/standard-ir/template.docx", dependencies=[Depends(require_api_key)])
def get_blank_template() -> Response:
    """The blank standard IR template (all options unselected)."""
    return Response(
        content=render_docx(None),
        media_type=_DOCX,
        headers={"Content-Disposition": 'attachment; filename="IR_Standard_Blank.docx"'},
    )
