"""Extraction results persistence."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import ResultRecord
from app.schemas.charge_sheet import ChargeSheetData, ReviewStatus
from app.schemas.raw import RawDocument
from app.schemas.structured import PATH_PREFIX


class ResultsRepository:
    def __init__(self, db: Session):
        self.db = db

    def save(self, document_id: str, data: ChargeSheetData, raw: Optional[RawDocument] = None) -> ResultRecord:
        """Store results; ``raw`` (when given) replaces the stored raw lines — a reprocess
        writes both, a correction only the results."""
        existing = self.get_record(document_id)
        payload = data.model_dump_json()
        raw_json = raw.model_dump_json() if raw is not None else None
        if existing:
            existing.payload_json = payload
            if raw is not None:
                existing.raw_json = raw_json
            existing.overall_confidence = data.overall_confidence
            existing.updated_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(existing)
            return existing
        row = ResultRecord(
            document_id=document_id,
            payload_json=payload,
            raw_json=raw_json,
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

    def get_raw(self, document_id: str) -> Optional[RawDocument]:
        row = self.get_record(document_id)
        if not row or not row.raw_json:
            return None
        return RawDocument.model_validate(json.loads(row.raw_json))

    def apply_corrections(self, document_id: str, corrections: dict) -> Optional[ChargeSheetData]:
        data = self.get(document_id)
        if not data:
            return None
        field_map = data.field_map()
        for name, corr in corrections.items():
            value = corr.get("corrected_value")
            status = corr.get("review_status", "corrected")
            try:
                status = ReviewStatus(status).value
            except ValueError:
                status = ReviewStatus.CORRECTED.value
            if name.startswith(PATH_PREFIX):  # a structured value, e.g. "s.accused.0.name"
                sv = data.structured.get_value(name[len(PATH_PREFIX):]) if data.structured else None
                if sv is None:
                    continue
                sv.corrected_value = None if value is None else str(value)
                sv.review_status = status
                if corr.get("notes"):
                    sv.review_notes = corr["notes"]
                continue
            field = field_map.get(name)
            if not field:
                continue
            field.corrected_value = None if value is None else str(value)
            field.review_status = ReviewStatus(status)
            if corr.get("notes"):
                field.notes = corr["notes"]
        self.save(document_id, data)
        return data
