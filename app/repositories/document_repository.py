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
    ) -> Optional[DocumentRecord]:
        doc = self.get(document_id)
        if not doc:
            return None
        doc.status = status.value
        doc.updated_at = datetime.utcnow()
        if error_message is not None:
            # Never store full document text in error fields
            doc.error_message = error_message[:500]
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
                    review_status=item.get("review_status", "corrected"),
                    notes=item.get("notes"),
                )
            )
        self.db.commit()

    def get_corrections(self, document_id: str) -> dict:
        rows = self.db.scalars(
            select(CorrectionRecord).where(CorrectionRecord.document_id == document_id)
        ).all()
        # Latest correction per field wins
        out: dict = {}
        for row in rows:
            out[row.canonical_name] = {
                "corrected_value": row.corrected_value,
                "review_status": row.review_status,
                "notes": row.notes,
                "created_at": row.created_at.isoformat(),
            }
        return out

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
            created_at=doc.created_at,
            updated_at=doc.updated_at,
            processed_at=doc.processed_at,
            schema_version=doc.schema_version,
        )
