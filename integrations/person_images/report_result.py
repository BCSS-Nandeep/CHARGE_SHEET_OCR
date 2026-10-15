"""The combined export: ``<output>/<document_id>/report_result.json``.

The OCR application's database stays the source of truth. This module only READS the latest persisted OCR state
(through the application's own repositories, exactly as ``GET /api/v1/documents/{id}/results`` does) and the
person-image manifest, and writes a derived file. It never writes to the database and never changes the OCR payload:
the payload is embedded as the application serialises it (``ResultsResponse``), with its own ``schema_version``.

Envelope (``schema_version`` 1.0.0, documented in ``report_result.schema.json``):

    {
      "schema_version": "1.0.0",
      "document_id": "...",
      "generated_at": "...Z",
      "processing": {"ocr_status", "ocr_document_status", "ocr_extraction_status", "ocr_error_summary",
                     "image_extraction_status", "image_extraction_error_summary"},
      "ocr":           {"schema_version": <the payload's own>, "payload": <ResultsResponse> | null},
      "person_images": {"status", "run_id", "total_images_found", "skipped_images", "unreadable_images",
                        "started_at", "finished_at", "images": [ {file, name, source_page, source_member,
                        width, height, faces, size_bytes, sha256} ]}
    }

Each pipeline's status stands on its own: a failed pipeline is reported as failed and the other one's data is kept;
when OCR has produced nothing, ``ocr.payload`` is ``null`` (nothing is reconstructed).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

from app import db as dbmod
from app.repositories.document_repository import DocumentRepository
from app.repositories.results_repository import ResultsRepository
from app.schemas.api_responses import ResultsResponse
from app.schemas.document import DocumentStatus

from . import REPORT_RESULT_SCHEMA_VERSION, store

_OCR_BUSY = (DocumentStatus.UPLOADED.value, DocumentStatus.QUEUED.value, DocumentStatus.PROCESSING.value)
_OCR_TERMINAL = (DocumentStatus.COMPLETED.value, DocumentStatus.FAILED.value, DocumentStatus.VERIFIED.value,
                 DocumentStatus.DELETED.value)
_watching: set = set()
_watch_lock = threading.Lock()


def _ocr_status(doc_status: str, extraction_status: Optional[str]) -> str:
    """The OCR pipeline's own status, independent of the images: processing / succeeded / partial / failed."""
    if doc_status in _OCR_BUSY:
        return "processing"
    if doc_status == DocumentStatus.FAILED.value:
        return "failed"
    if doc_status in (DocumentStatus.COMPLETED.value, DocumentStatus.VERIFIED.value):
        return "partial" if extraction_status == "partial" else ("failed" if extraction_status == "failed" else "succeeded")
    return "unknown"


def _images_section(document_id: str) -> Dict[str, Any]:
    summary = store.summarize(store.read_manifest(document_id))
    latest, current = summary["latest"], summary["current"]
    if latest is None:
        return {"status": "not_started", "run_id": None, "total_images_found": 0, "skipped_images": 0,
                "unreadable_images": 0, "started_at": None, "finished_at": None, "images": []}
    shown = current or latest
    images = [
        {k: im.get(k) for k in ("file", "name", "source_page", "source_member", "width", "height", "faces",
                                "size_bytes", "sha256")}
        for im in (shown.get("images") or [])
    ]
    return {
        "status": latest.get("status"),  # the LATEST run's outcome (a failed re-run is not hidden)
        "run_id": shown.get("run_id"),   # whose images are listed (the newest usable run)
        "latest_run_id": latest.get("run_id"),
        "total_images_found": shown.get("total_images_found", 0),
        "skipped_images": shown.get("skipped_images", 0),
        "unreadable_images": shown.get("unreadable_images", 0),
        "started_at": shown.get("started_at"),
        "finished_at": shown.get("finished_at"),
        "images": images,
    }


def build(document_id: str) -> Optional[Dict[str, Any]]:
    """The combined document for the latest persisted state, or None when the document does not exist / is deleted."""
    with dbmod.session_scope() as db:
        repo = DocumentRepository(db)
        doc = repo.get(document_id)
        if doc is None or doc.status == DocumentStatus.DELETED.value:
            return None
        meta = repo.to_meta(doc)
        results = ResultsRepository(db).get(document_id) if doc.status not in _OCR_BUSY else None
        corrections = repo.get_corrections(document_id)
        doc_status, error_message = doc.status, (doc.error_message or None)

    payload = None
    extraction_status = None
    ocr_schema = None
    if results is not None and doc_status != DocumentStatus.FAILED.value:
        # exactly what GET /results returns; nothing renamed, dropped or recomputed
        payload = ResultsResponse(document=meta, results=results, field_corrections=corrections).model_dump(mode="json")
        extraction_status = results.extraction_status.value
        ocr_schema = results.schema_version

    images = _images_section(document_id)
    latest = store.summarize(store.read_manifest(document_id))["latest"] or {}
    ocr_status = _ocr_status(doc_status, extraction_status)
    return {
        "schema_version": REPORT_RESULT_SCHEMA_VERSION,
        "document_id": document_id,
        "generated_at": store.utcnow(),
        "processing": {
            "ocr_status": ocr_status,
            "ocr_document_status": doc_status,
            "ocr_extraction_status": extraction_status,
            "ocr_error_summary": (error_message[:300] if error_message and ocr_status == "failed" else None),
            "image_extraction_status": images["status"],
            "image_extraction_error_summary": latest.get("error"),
        },
        "ocr": {"schema_version": ocr_schema, "payload": payload},
        "person_images": images,
    }


def original_name(document_id: str) -> str:
    """The uploaded file's name (from the OCR database, read-only); a neutral name if the document is unknown."""
    try:
        with dbmod.session_scope() as db:
            doc = DocumentRepository(db).get(document_id)
            if doc is not None and doc.original_filename:
                return doc.original_filename
    except Exception:  # noqa: BLE001
        pass
    return f"document {document_id[:8]}"


def refresh(document_id: str) -> Optional[Dict[str, Any]]:
    """Rebuild from the latest persisted state and write ``report_result.json`` (atomically). None if no such document.

    The file goes into the document's output folder, which is named after the uploaded file."""
    data = build(document_id)
    if data is None:
        return None
    folder = store.ensure_dir(document_id, original_name(document_id))
    store.write_json_atomic(folder / "report_result.json", data)
    return data


def refresh_quietly(document_id: str) -> None:
    """For background use: a failure here must never affect the OCR or the image pipeline."""
    try:
        refresh(document_id)
    except Exception:  # noqa: BLE001
        pass


def watch_ocr(document_id: str, poll_seconds: float = 3.0, max_seconds: float = 3 * 3600) -> None:
    """Refresh the combined file once the OCR pipeline reaches a final state (it runs in its own thread).

    Polls every second for the first two minutes (a DOCX is usually done within seconds), then every ``poll_seconds``."""
    with _watch_lock:
        if document_id in _watching:
            return
        _watching.add(document_id)

    def loop() -> None:
        try:
            started = time.monotonic()
            deadline = started + max_seconds
            while time.monotonic() < deadline:
                with dbmod.session_scope() as db:
                    doc = DocumentRepository(db).get(document_id)
                    status = doc.status if doc else DocumentStatus.DELETED.value
                if status in _OCR_TERMINAL:
                    refresh_quietly(document_id)
                    return
                time.sleep(1.0 if time.monotonic() - started < 120 else poll_seconds)
        finally:
            with _watch_lock:
                _watching.discard(document_id)

    threading.Thread(target=loop, name=f"person-images-watch-{document_id[:8]}", daemon=True).start()
