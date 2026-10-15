"""Person-image API (new routes only; the OCR routes are untouched).

    GET  /api/v1/documents/{id}/person-images                      status + images of the document
    GET  /api/v1/documents/{id}/person-images/files/{run}/{name}   one image (auth header required, like the rest)
    POST /api/v1/documents/{id}/person-images/run                  run the image extraction again (operator)
    GET  /api/v1/documents/{id}/report_result                      refresh + return the combined JSON (?download=true)

Authentication and roles are the application's own (``require_api_key`` = any valid caller, ``require_role``).
"""

from __future__ import annotations

import json
from typing import Any, Dict
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.logging_utils import audit
from app.repositories.document_repository import DocumentRepository
from app.schemas.document import DocumentStatus
from app.security.auth import Principal, require_api_key, require_role

from . import report_result, service, store

router = APIRouter(prefix="/api/v1/documents", tags=["person-images"])

_MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
          ".bmp": "image/bmp", ".webp": "image/webp", ".tif": "image/tiff", ".tiff": "image/tiff"}


def _live(db: Session, document_id: str):
    if not store.DOC_ID.match(document_id):
        raise HTTPException(status_code=404, detail="Document not found")
    doc = DocumentRepository(db).get(document_id)
    if not doc or doc.status == DocumentStatus.DELETED.value:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


def public_view(document_id: str) -> Dict[str, Any]:
    """What the UI shows: latest status, the images of the newest usable run (with API urls), the run history."""
    summary = store.summarize(store.read_manifest(document_id))
    latest, current = summary["latest"], summary["current"]
    if latest is None:
        return {"document_id": document_id, "status": "not_started", "message": "Image extraction has not run for this document.",
                "run_id": None, "latest_run_id": None, "images": [], "runs": []}
    shown = current or latest
    images = []
    for im in shown.get("images") or []:
        run_id = shown["run_id"]
        available = store.resolve_image(document_id, run_id, im.get("name", "")) is not None
        images.append({
            "name": im.get("name"), "file": im.get("file"), "index": im.get("index"),
            "source_page": im.get("source_page"), "source_member": im.get("source_member"),
            "width": im.get("width"), "height": im.get("height"), "faces": im.get("faces"),
            "size_bytes": im.get("size_bytes"), "available": available,
            "url": (f"/api/v1/documents/{document_id}/person-images/files/{run_id}/{quote(str(im.get('name')), safe='')}"
                    if available else None),
        })
    return {
        "document_id": document_id,
        "status": latest.get("status"),
        "error": latest.get("error"),
        "run_id": shown.get("run_id"),
        "latest_run_id": latest.get("run_id"),
        "total_images_found": shown.get("total_images_found", 0),
        "skipped_images": shown.get("skipped_images", 0),
        "unreadable_images": shown.get("unreadable_images", 0),
        "images": images,
        "runs": [{k: r.get(k) for k in ("run_id", "status", "queued_at", "started_at", "finished_at", "error")}
                 for r in summary["runs"]],
    }


@router.get("/{document_id}/person-images", dependencies=[Depends(require_api_key)])
def get_person_images(document_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    _live(db, document_id)
    return public_view(document_id)


@router.get("/{document_id}/person-images/files/{run_id}/{filename}", dependencies=[Depends(require_api_key)])
def get_person_image_file(document_id: str, run_id: str, filename: str, db: Session = Depends(get_db)):
    _live(db, document_id)
    path = store.resolve_image(document_id, run_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Image not available")
    return FileResponse(path, media_type=_MEDIA.get(path.suffix.lower(), "application/octet-stream"),
                        filename=path.name, content_disposition_type="inline",
                        headers={"Cache-Control": "no-store"})


@router.post("/{document_id}/person-images/run", status_code=202)
def run_person_images(
    document_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_role("operator")),
) -> Dict[str, Any]:
    """Run the image extraction again. OCR is not touched; earlier runs and their images are kept."""
    _live(db, document_id)
    summary = store.summarize(store.read_manifest(document_id))
    if summary["latest"] and summary["latest"].get("status") in store.ACTIVE:
        raise HTTPException(status_code=409, detail="Image extraction is already running for this document")
    run_id = service.start(document_id)
    audit("person_images_run_requested", document_id=document_id, user=principal.name)
    return {"document_id": document_id, "run_id": run_id, "status": store.QUEUED}


@router.get("/{document_id}/report_result", dependencies=[Depends(require_api_key)])
def get_report_result(document_id: str, download: bool = False, db: Session = Depends(get_db)):
    """The combined OCR + person-image JSON, rebuilt from the latest persisted state (and saved to the output folder)."""
    _live(db, document_id)
    data = report_result.refresh(document_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if not download:
        return data
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="report_result.json"'},
    )
