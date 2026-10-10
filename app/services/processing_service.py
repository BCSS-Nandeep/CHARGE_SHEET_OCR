"""Async-capable document processing orchestration."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from app.config import get_settings
from app.db import session_scope
from app.logging_utils import audit, get_logger, safe_exc_message
from app.repositories.document_repository import DocumentRepository
from app.repositories.results_repository import ResultsRepository
from app.schemas.charge_sheet import SCHEMA_VERSION, ExtractionStatus
from app.schemas.document import DocumentStatus
from app.security.files import resolve_under
from app.services.extraction_service import ExtractionService

logger = get_logger(__name__)
_executor = ThreadPoolExecutor(max_workers=2)
_lock = threading.Lock()
_inflight = set()


def enqueue_processing(document_id: str) -> None:
    settings = get_settings()
    with _lock:
        if document_id in _inflight:
            return
        _inflight.add(document_id)

    if settings.async_processing:
        _executor.submit(_run_safe, document_id)
    else:
        _run_safe(document_id)


def is_inflight(document_id: str) -> bool:
    with _lock:
        return document_id in _inflight


def recover_interrupted() -> int:
    """Documents left QUEUED/PROCESSING by a restart would never finish — mark them failed."""
    count = 0
    with session_scope() as db:
        repo = DocumentRepository(db)
        for doc in repo.list_by_status([DocumentStatus.QUEUED, DocumentStatus.PROCESSING]):
            if is_inflight(doc.id):
                continue
            repo.update_status(
                doc.id,
                DocumentStatus.FAILED,
                error_message="Processing was interrupted (service restarted). Use Reprocess to run it again.",
            )
            count += 1
    if count:
        audit("recovered_interrupted", count=count)
    return count


def _run_safe(document_id: str) -> None:
    try:
        process_document(document_id)
    except Exception as exc:
        logger.error("processing_unhandled document_id=%s err=%s", document_id, safe_exc_message(exc))
        try:
            with session_scope() as db:
                DocumentRepository(db).update_status(
                    document_id,
                    DocumentStatus.FAILED,
                    error_message=safe_exc_message(exc),
                )
        except Exception:
            pass
    finally:
        with _lock:
            _inflight.discard(document_id)


def _failure_reason(data) -> str:
    for prefix in ("OCR_REQUIRED", "EXTRACTION_FAILED"):
        for w in data.extraction_warnings:
            if w.startswith(prefix):
                return w[:480]
    return "Extraction failed: no usable text could be obtained"


def process_document(document_id: str) -> None:
    settings = get_settings()
    audit("process_start", document_id=document_id)

    with session_scope() as db:
        repo = DocumentRepository(db)
        doc = repo.get(document_id)
        if not doc:
            return
        repo.update_status(document_id, DocumentStatus.PROCESSING, error_message="")
        stored = doc.stored_filename

    path = resolve_under(settings.upload_dir, stored)
    if not path.exists():
        with session_scope() as db:
            DocumentRepository(db).update_status(
                document_id,
                DocumentStatus.FAILED,
                error_message="Stored PDF not found",
            )
        return

    try:
        with session_scope() as db:
            data = ExtractionService(db=db).process(path)
            repo = DocumentRepository(db)
            reapplied, skipped = repo.reapply_corrections(document_id, data)
            if skipped:
                data.extraction_warnings.append(
                    f"{skipped} earlier correction(s) were not re-applied because the source value changed; "
                    "see the correction history"
                )
            ResultsRepository(db).save(document_id, data)
            failed = data.extraction_status == ExtractionStatus.FAILED
            repo.update_status(
                document_id,
                DocumentStatus.FAILED if failed else DocumentStatus.COMPLETED,
                error_message=_failure_reason(data) if failed else "",
                page_count=len(data.page_summaries),
                is_scanned=any(p.text_source != "native" for p in data.page_summaries),
                template_id=data.document_info.template_id,
                classification_confidence=data.document_info.classification_confidence,
                requires_review=True,
                verified=False,  # new extraction output has not been verified by anyone
                schema_version=SCHEMA_VERSION,
            )
        audit(
            "process_failed" if failed else "process_success",
            document_id=document_id,
            pages=len(data.page_summaries),
            extraction_status=data.extraction_status.value,
            corrections_reapplied=reapplied,
        )
    except Exception as exc:
        with session_scope() as db:
            DocumentRepository(db).update_status(
                document_id,
                DocumentStatus.FAILED,
                error_message=safe_exc_message(exc),
            )
        audit("process_failed", document_id=document_id, error=type(exc).__name__)
        raise
