"""Configurable retention / deletion of uploaded documents."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.logging_utils import audit, get_logger
from app.models import DocumentRecord
from app.repositories.document_repository import DocumentRepository
from app.schemas.document import DocumentStatus
from app.security.files import resolve_under

logger = get_logger(__name__)


def delete_document_content(repo: DocumentRepository, doc: DocumentRecord, upload_dir: Path) -> None:
    """Delete the stored PDF, extracted results and corrections.

    A tombstone row (id, timestamps, status=deleted) is kept for the audit
    trail; it holds no document content or personal data.
    """
    try:
        path = resolve_under(upload_dir, doc.stored_filename)
        if path.exists():
            path.unlink()
    except Exception as exc:
        logger.warning("delete_file_failed document_id=%s err=%s", doc.id, type(exc).__name__)
    repo.purge_content(doc)


def purge_expired() -> int:
    settings = get_settings()
    if settings.retention_days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=settings.retention_days)
    deleted = 0
    with session_scope() as db:
        rows = list(
            db.scalars(
                select(DocumentRecord).where(
                    DocumentRecord.created_at < cutoff,
                    DocumentRecord.status != DocumentStatus.DELETED.value,
                )
            )
        )
        repo = DocumentRepository(db)
        for doc in rows:
            delete_document_content(repo, doc, settings.upload_dir)
            deleted += 1
    if deleted:
        audit("retention_purge", count=deleted)
    return deleted
