"""Async-capable document processing orchestration."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app.db import session_scope
from app.logging_utils import audit, get_logger, safe_exc_message
from app.repositories.document_repository import DocumentRepository
from app.repositories.results_repository import ResultsRepository
from app.schemas.charge_sheet import SCHEMA_VERSION
from app.schemas.document import DocumentStatus
from app.security.files import resolve_under
from app.services.extraction_service import ExtractionService
from app.services.pdf_service import PdfService

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


def process_document(document_id: str) -> None:
    settings = get_settings()
    audit("process_start", document_id=document_id)

    with session_scope() as db:
        repo = DocumentRepository(db)
        doc = repo.get(document_id)
        if not doc:
            return
        repo.update_status(document_id, DocumentStatus.PROCESSING)
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
        # Quick page count
        pdf_meta = PdfService().extract(path)
        with session_scope() as db:
            data = ExtractionService(db=db).process(path)
            ResultsRepository(db).save(document_id, data)
            DocumentRepository(db).update_status(
                document_id,
                DocumentStatus.COMPLETED,
                page_count=pdf_meta.page_count,
                is_scanned=pdf_meta.is_scanned,
                template_id=data.document_info.template_id,
                classification_confidence=data.document_info.classification_confidence,
                requires_review=data.requires_human_review,
                schema_version=SCHEMA_VERSION,
            )
        audit(
            "process_success",
            document_id=document_id,
            pages=pdf_meta.page_count,
            confidence=data.overall_confidence,
        )
    except Exception as exc:
        with session_scope() as db:
            DocumentRepository(db).update_status(
                document_id,
                DocumentStatus.FAILED,
                error_message=safe_exc_message(exc),
            )
        audit("process_failed", document_id=document_id, error=safe_exc_message(exc))
        raise
