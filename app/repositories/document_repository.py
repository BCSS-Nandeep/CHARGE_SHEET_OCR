"""Document persistence."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CorrectionRecord, DocumentRecord
from app.schemas.document import DocumentMeta, DocumentStatus


class DocumentRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        *,
        original_filename: str,
        stored_filename: str,
        content_type: str,
        size_bytes: int,
    ) -> DocumentRecord:
        doc = DocumentRecord(
            id=str(uuid.uuid4()),
            original_filename=original_filename,
            stored_filename=stored_filename,
            content_type=content_type,
            size_bytes=size_bytes,
            status=DocumentStatus.UPLOADED.value,
        )
        self.db.add(doc)
        self.db.commit()
        self.db.refresh(doc)
        return doc

    def get(self, document_id: str) -> Optional[DocumentRecord]:
        return self.db.get(DocumentRecord, document_id)

    def update_status(
        self,
        document_id: str,
        status: DocumentStatus,
        *,
        error_message: Optional[str] = None,
        page_count: Optional[int] = None,
        is_scanned: Optional[bool] = None,
        template_id: Optional[str] = None,
        classification_confidence: Optional[float] = None,
        requires_review: Optional[bool] = None,
        schema_version: Optional[str] = None,
        verified: Optional[bool] = None,
        verified_by: Optional[str] = None,
    ) -> Optional[DocumentRecord]:
        doc = self.get(document_id)
        if not doc:
            return None
        doc.status = status.value
        doc.updated_at = datetime.utcnow()
        if error_message is not None:
            # Never store full document text in error fields
            doc.error_message = error_message[:500] or None
        if page_count is not None:
            doc.page_count = page_count
        if is_scanned is not None:
            doc.is_scanned = is_scanned
        if template_id is not None:
            doc.template_id = template_id
        if classification_confidence is not None:
            doc.classification_confidence = classification_confidence
        if requires_review is not None:
            doc.requires_review = requires_review
        if schema_version is not None:
            doc.schema_version = schema_version
        if verified is not None:
            doc.verified = verified
            doc.verified_by = verified_by if verified else None
        if status in (DocumentStatus.COMPLETED, DocumentStatus.FAILED, DocumentStatus.VERIFIED):
            doc.processed_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(doc)
        return doc

    def list(
        self,
        *,
        status: Optional[DocumentStatus] = None,
        verified: Optional[bool] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[int, List[DocumentRecord]]:
        stmt = select(DocumentRecord).where(DocumentRecord.status != DocumentStatus.DELETED.value)
        count_stmt = select(func.count()).select_from(DocumentRecord).where(
            DocumentRecord.status != DocumentStatus.DELETED.value
        )
        if status:
            stmt = stmt.where(DocumentRecord.status == status.value)
            count_stmt = count_stmt.where(DocumentRecord.status == status.value)
        if verified is not None:
            stmt = stmt.where(DocumentRecord.verified == verified)
            count_stmt = count_stmt.where(DocumentRecord.verified == verified)
        total = self.db.scalar(count_stmt) or 0
        rows = list(
            self.db.scalars(
                stmt.order_by(DocumentRecord.created_at.desc()).limit(limit).offset(offset)
            )
        )
        return total, rows

    def list_by_status(self, statuses: List[DocumentStatus]) -> List[DocumentRecord]:
        return list(
            self.db.scalars(
                select(DocumentRecord).where(DocumentRecord.status.in_([s.value for s in statuses]))
            )
        )

    def add_corrections(
        self,
        document_id: str,
        corrections: List[dict],
    ) -> None:
        for item in corrections:
            self.db.add(
                CorrectionRecord(
                    document_id=document_id,
                    canonical_name=item["canonical_name"],
                    corrected_value=str(item["corrected_value"]),
                    original_value=item.get("original_value"),
                    review_status=item.get("review_status", "corrected"),
                    notes=item.get("notes"),
                    reviewer=item.get("reviewer"),
                )
            )
        self.db.commit()

    def _correction_rows(self, document_id: str) -> List[CorrectionRecord]:
        return list(
            self.db.scalars(
                select(CorrectionRecord)
                .where(CorrectionRecord.document_id == document_id)
                .order_by(CorrectionRecord.id)
            )
        )

    def get_corrections(self, document_id: str) -> dict:
        """Latest correction per field (rows are ordered by insertion)."""
        out: dict = {}
        for row in self._correction_rows(document_id):
            out[row.canonical_name] = {
                "corrected_value": row.corrected_value,
                "original_value": row.original_value,
                "review_status": row.review_status,
                "notes": row.notes,
                "reviewer": row.reviewer,
                "created_at": row.created_at.isoformat(),
            }
        return out

    def correction_history(self, document_id: str) -> List[dict]:
        return [
            {
                "canonical_name": r.canonical_name,
                "corrected_value": r.corrected_value,
                "original_value": r.original_value,
                "review_status": r.review_status,
                "notes": r.notes,
                "reviewer": r.reviewer,
                "created_at": r.created_at.isoformat(),
            }
            for r in self._correction_rows(document_id)
        ]

    def reapply_corrections(self, document_id: str, data) -> Tuple[int, int]:
        """Apply stored human corrections to a fresh extraction.

        A correction is re-applied only when the field still exists and its
        extracted value equals the value the reviewer corrected; otherwise the
        source changed and the reviewer must look again. Returns (applied, skipped).
        """
        from app.schemas.charge_sheet import ReviewStatus

        fmap = data.field_map()
        applied = skipped = 0
        for name, corr in self.get_corrections(document_id).items():
            if name.startswith("_"):
                continue
            field = fmap.get(name)
            if field is None or corr.get("original_value") is None or str(field.original_value) != corr["original_value"]:
                skipped += 1
                continue
            field.corrected_value = corr["corrected_value"]
            try:
                field.review_status = ReviewStatus(corr["review_status"])
            except ValueError:
                field.review_status = ReviewStatus.CORRECTED
            if corr.get("notes"):
                field.notes = corr["notes"]
            applied += 1
        return applied, skipped

    def purge_content(self, doc: DocumentRecord) -> None:
        """Remove extracted data and corrections; keep a minimal tombstone row."""
        if doc.result is not None:
            self.db.delete(doc.result)
        for c in list(doc.corrections):
            self.db.delete(c)
        doc.original_filename = "[purged]"
        doc.error_message = None
        doc.status = DocumentStatus.DELETED.value
        doc.updated_at = datetime.utcnow()

    @staticmethod
    def to_meta(doc: DocumentRecord) -> DocumentMeta:
        return DocumentMeta(
            id=doc.id,
            original_filename=doc.original_filename,
            stored_filename=doc.stored_filename,
            content_type=doc.content_type,
            size_bytes=doc.size_bytes,
            page_count=doc.page_count,
            status=DocumentStatus(doc.status),
            error_message=doc.error_message,
            is_scanned=doc.is_scanned,
            template_id=doc.template_id,
            classification_confidence=doc.classification_confidence,
            requires_review=doc.requires_review,
            verified=doc.verified,
            verified_by=doc.verified_by,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
            processed_at=doc.processed_at,
            schema_version=doc.schema_version,
        )
