"""Extraction results persistence."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import ResultRecord
from app.schemas.charge_sheet import ChargeSheetData, ReviewStatus


class ResultsRepository:
    def __init__(self, db: Session):
        self.db = db

    def save(self, document_id: str, data: ChargeSheetData) -> ResultRecord:
        existing = self.get_record(document_id)
        payload = data.model_dump_json()
        if existing:
            existing.payload_json = payload
            existing.overall_confidence = data.overall_confidence
            existing.updated_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(existing)
            return existing
        row = ResultRecord(
            document_id=document_id,
            payload_json=payload,
            overall_confidence=data.overall_confidence,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def get_record(self, document_id: str) -> Optional[ResultRecord]:
        return (
            self.db.query(ResultRecord)
            .filter(ResultRecord.document_id == document_id)
            .one_or_none()
        )

    def get(self, document_id: str) -> Optional[ChargeSheetData]:
        row = self.get_record(document_id)
        if not row:
            return None
        return ChargeSheetData.model_validate(json.loads(row.payload_json))

    def apply_corrections(self, document_id: str, corrections: dict) -> Optional[ChargeSheetData]:
        data = self.get(document_id)
        if not data:
            return None
        field_map = data.field_map()
        for name, corr in corrections.items():
            field = field_map.get(name)
            if not field:
                continue
            field.corrected_value = corr.get("corrected_value")
            status = corr.get("review_status", "corrected")
            try:
                field.review_status = ReviewStatus(status)
            except ValueError:
                field.review_status = ReviewStatus.CORRECTED
            if corr.get("notes"):
                field.notes = corr["notes"]
        self.save(document_id, data)
        return data
