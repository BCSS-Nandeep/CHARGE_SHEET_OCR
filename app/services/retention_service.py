"""Configurable retention / deletion of uploaded documents."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.logging_utils import audit, get_logger
from app.models import DocumentRecord
from app.schemas.document import DocumentStatus
from app.security.files import resolve_under

logger = get_logger(__name__)


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
        for doc in rows:
            try:
                path = resolve_under(settings.upload_dir, doc.stored_filename)
                if path.exists():
                    path.unlink()
            except Exception:
                pass
            doc.status = DocumentStatus.DELETED.value
            doc.updated_at = datetime.utcnow()
            deleted += 1
        # cascade deletes results via ORM relationships when we delete rows;
        # here we soft-delete to keep audit trail
    if deleted:
        audit("retention_purge", count=deleted)
    return deleted
