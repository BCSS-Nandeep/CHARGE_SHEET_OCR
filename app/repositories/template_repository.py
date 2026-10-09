"""Optional template fingerprint storage."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TemplateRecord


class TemplateRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_fingerprint(self, fingerprint: str) -> Optional[TemplateRecord]:
        return self.db.scalars(
            select(TemplateRecord).where(TemplateRecord.fingerprint == fingerprint)
        ).first()

    def upsert(
        self,
        fingerprint: str,
        *,
        label: Optional[str] = None,
        mapping_json: Optional[str] = None,
        verified: bool = False,
    ) -> TemplateRecord:
        row = self.get_by_fingerprint(fingerprint)
        if row:
            row.document_count = (row.document_count or 0) + 1
            row.last_seen_at = datetime.utcnow()
            if label:
                row.label = label
            if mapping_json:
                row.mapping_json = mapping_json
            if verified:
                row.verified = True
            self.db.commit()
            self.db.refresh(row)
            return row
        row = TemplateRecord(
            id=str(uuid.uuid4()),
            fingerprint=fingerprint,
            label=label,
            mapping_json=mapping_json,
            document_count=1,
            verified=verified,
            last_seen_at=datetime.utcnow(),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def list_all(self) -> List[TemplateRecord]:
        return list(
            self.db.scalars(
                select(TemplateRecord).order_by(TemplateRecord.last_seen_at.desc())
            )
        )
