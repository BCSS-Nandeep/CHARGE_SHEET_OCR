"""Deterministic validation of extracted charge-sheet fields."""

from __future__ import annotations

import re
from typing import List, Optional

from app.schemas.charge_sheet import (
    ChargeSheetData,
    ExtractedField,
    ValidationStatus,
)

_DATE = re.compile(
    r"^(\d{1,2}[-/\.]\d{1,2}[-/\.]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})$"
)
_YEAR = re.compile(r"^(19|20)\d{2}$")
_FIR = re.compile(r"^[\w\-/\.]{1,40}$")


class ValidationService:
    def validate(self, data: ChargeSheetData) -> ChargeSheetData:
        warnings: List[str] = list(data.extraction_warnings or [])
        for field in data.all_fields():
            self._validate_field(field)

        # Case-level checks
        case = data.case
        if case.fir_number and not (case.fir_number.original_value or case.fir_number.normalized_value):
            case.fir_number.validation_status = ValidationStatus.MISSING

        filled = [
            f
            for f in [
                case.district,
                case.police_station,
                case.fir_number,
                case.charge_sheet_number,
            ]
            if f and (f.original_value or f.normalized_value)
        ]
        if len(filled) < 2:
            warnings.append("Few core case identifiers found; document may need manual review")
            data.requires_human_review = True

        if data.document_info.classification_confidence is not None:
            if data.document_info.classification_confidence < 0.55:
                warnings.append("Document classification confidence is low")
                data.requires_human_review = True

        if not data.accused_persons:
            warnings.append("No accused persons extracted")
            data.requires_human_review = True

        # Overall confidence
        fields = [f for f in data.all_fields() if f.original_value or f.normalized_value]
        if fields:
            confs = [
                ((f.extraction_confidence or 0.5) + (f.mapping_confidence or 0.5)) / 2
                for f in fields
            ]
            data.overall_confidence = round(sum(confs) / len(confs), 3)
        else:
            data.overall_confidence = 0.0
            data.requires_human_review = True
            warnings.append("No fields extracted")

        data.extraction_warnings = warnings
        return data

    def _validate_field(self, field: ExtractedField) -> None:
        # Never overwrite an explicit source conflict
        if field.validation_status == ValidationStatus.CONFLICTING:
            return

        value = field.normalized_value if field.normalized_value is not None else field.original_value
        if value is None or str(value).strip() == "":
            field.validation_status = ValidationStatus.MISSING
            return

        name = field.canonical_name
        text = str(value).strip()

        if name in {"fir_date", "submission_date", "seizure_date", "report_date"}:
            if _DATE.match(text) or re.search(r"\d{1,2}[-/\.]\d{1,2}[-/\.]\d{2,4}", text):
                field.validation_status = ValidationStatus.VALID
                field.normalized_value = text
            else:
                field.validation_status = ValidationStatus.UNCERTAIN
            return

        if name == "year":
            m = re.search(r"(19|20)\d{2}", text)
            if m:
                field.normalized_value = m.group(0)
                field.validation_status = ValidationStatus.VALID
            else:
                field.validation_status = ValidationStatus.UNCERTAIN
            return

        if name in {"fir_number", "crime_number", "charge_sheet_number"}:
            if _FIR.match(text):
                field.validation_status = ValidationStatus.VALID
            else:
                field.validation_status = ValidationStatus.UNCERTAIN
            return

        # Case identifiers with person-like / narrative values stay uncertain
        if field.canonical_name in {
            "district",
            "police_station",
            "year",
            "fir_number",
            "fir_date",
            "submission_date",
        }:
            if re.search(r"\b(?:s/o|age\s*:|caste|yrs?|confess|apprehend)\b", text, re.I):
                field.validation_status = ValidationStatus.UNCERTAIN
                return

        if (field.mapping_confidence or 1) < 0.8 or (field.extraction_confidence or 1) < 0.6:
            field.validation_status = ValidationStatus.UNCERTAIN
        else:
            field.validation_status = ValidationStatus.VALID


def normalize_simple(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None
