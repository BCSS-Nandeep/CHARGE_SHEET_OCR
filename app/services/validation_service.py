"""Deterministic validation of extracted charge-sheet fields.

Validation status describes the *value* (format / consistency). It is kept
separate from extraction status (did the pipeline run) and review status
(has a human checked it). Original values are never modified.
"""

from __future__ import annotations

import re
from datetime import date
from typing import List, Optional

from app.schemas.charge_sheet import (
    ChargeSheetData,
    ExtractedField,
    ExtractionStatus,
    ValidationStatus,
)
from app.services.value_parsers import (
    digits,
    is_valid_aadhaar_shape,
    is_valid_indian_mobile,
    parse_date,
)

_FIR = re.compile(r"^\s*(?:[A-Za-z.\-]{0,10}\s*)?(\d{1,6})\s*/\s*(\d{4}|\d{2})\.?\s*$")
_DATE_FIELDS = {"fir_date", "submission_date", "seizure_date", "report_date"}
_STATUS_WEIGHT = {
    ValidationStatus.VALID: 1.0,
    ValidationStatus.UNCERTAIN: 0.6,
    ValidationStatus.CONFLICTING: 0.5,
    ValidationStatus.INVALID: 0.2,
    ValidationStatus.PENDING: 0.6,
}
CORE_CASE_FIELDS = ("district", "police_station", "fir_number", "fir_date")


def _attr(canonical: str) -> str:
    return canonical.rsplit(".", 1)[-1]


def _note(field: ExtractedField, text: str) -> None:
    if text not in (field.notes or ""):
        field.notes = f"{field.notes}; {text}" if field.notes else text


class ValidationService:
    def validate(self, data: ChargeSheetData) -> ChargeSheetData:
        warnings: List[str] = list(data.extraction_warnings or [])
        for f in data.all_fields():
            self._validate_field(f)
        self._cross_checks(data, warnings)

        missing = [k for k in CORE_CASE_FIELDS if getattr(data.case, k) is None]
        if missing and data.extraction_status != ExtractionStatus.FAILED:
            warnings.append("Core case fields not found in the source: " + ", ".join(missing))
        if (
            data.extraction_status != ExtractionStatus.FAILED
            and data.document_info.classification_confidence is not None
            and data.document_info.classification_confidence < 0.55
        ):
            warnings.append("Document classification confidence is low")
        if not data.accused_persons and data.extraction_status != ExtractionStatus.FAILED:
            warnings.append("No accused persons extracted")
        if not data.acts_and_sections and data.extraction_status != ExtractionStatus.FAILED:
            warnings.append("No acts/sections extracted")

        data.overall_confidence = self._confidence(data)
        data.requires_human_review = True
        data.extraction_warnings = list(dict.fromkeys(warnings))
        return data

    # ------------------------------------------------------------------

    def _validate_field(self, f: ExtractedField) -> None:
        if f.validation_status == ValidationStatus.CONFLICTING:
            return
        value = f.original_value
        if value is None or str(value).strip() == "":
            f.validation_status = ValidationStatus.MISSING
            return
        text = re.sub(r"\s+", " ", str(value)).strip()
        attr = _attr(f.canonical_name)

        if attr in _DATE_FIELDS:
            d = parse_date(text)
            if d is None:
                f.validation_status = ValidationStatus.INVALID
                _note(f, "not a valid calendar date")
                return
            f.normalized_value = d.isoformat()
            if d > date.today():
                f.validation_status = ValidationStatus.UNCERTAIN
                _note(f, "date is in the future")
            elif d.year < 1950:
                f.validation_status = ValidationStatus.UNCERTAIN
                _note(f, "implausibly old date")
            else:
                f.validation_status = ValidationStatus.VALID
            return

        if attr == "year":
            m = re.search(r"(?<!\d)(19|20)\d{2}(?!\d)", text)
            if m and 1950 <= int(m.group(0)) <= date.today().year + 1:
                f.normalized_value = m.group(0)
                f.validation_status = ValidationStatus.VALID
            else:
                f.validation_status = ValidationStatus.INVALID
                _note(f, "not a plausible year")
            return

        if attr in {"fir_number", "crime_number"}:
            m = _FIR.match(text)
            if m:
                f.normalized_value = f"{int(m.group(1))}/{m.group(2)}"
                f.validation_status = ValidationStatus.VALID
            elif re.search(r"\d", text):
                f.validation_status = ValidationStatus.UNCERTAIN
                _note(f, "unexpected FIR/crime number format")
            else:
                f.validation_status = ValidationStatus.INVALID
                _note(f, "contains no number")
            return

        if attr == "charge_sheet_number":
            if re.match(r"^\s*/", text) or not re.search(r"\d", text.split("/")[0]):
                f.validation_status = ValidationStatus.INVALID
                _note(f, "serial number is blank in the source (only the year is filled in)")
            else:
                f.validation_status = ValidationStatus.VALID
            return

        if attr == "age":
            if text.isdigit() and 1 <= int(text) <= 120:
                f.validation_status = ValidationStatus.VALID
            else:
                f.validation_status = ValidationStatus.INVALID
                _note(f, "not a plausible age")
            return

        if attr == "contact":
            if is_valid_indian_mobile(text):
                f.normalized_value = digits(text)[-10:]
                f.validation_status = ValidationStatus.VALID
            else:
                f.validation_status = ValidationStatus.INVALID
                _note(f, f"not a valid 10-digit mobile number ({len(digits(text))} digits)")
            return

        if attr == "identification":
            if is_valid_aadhaar_shape(text):
                f.validation_status = ValidationStatus.VALID
            else:
                f.validation_status = ValidationStatus.UNCERTAIN
                _note(f, "identification number format not recognised")
            return

        if attr in {"district", "police_station"}:
            from app.services.form_parser import plausible

            f.validation_status = ValidationStatus.VALID if plausible(attr, text) else ValidationStatus.UNCERTAIN
            if f.validation_status == ValidationStatus.UNCERTAIN:
                _note(f, "value does not look like a place name")
            return

        if attr == "name":
            if re.search(r"\d", text) or len(text.split()) > 7 or not re.search(r"[A-Za-z]{2,}", text):
                f.validation_status = ValidationStatus.UNCERTAIN
                _note(f, "value does not look like a person name")
                return

        if attr == "document_type" and text == "unknown":
            f.validation_status = ValidationStatus.UNCERTAIN
            return

        if (f.mapping_confidence if f.mapping_confidence is not None else 0.5) < 0.8 or (
            f.extraction_confidence if f.extraction_confidence is not None else 0.5
        ) < 0.6:
            f.validation_status = ValidationStatus.UNCERTAIN
        else:
            f.validation_status = ValidationStatus.VALID

    def _cross_checks(self, data: ChargeSheetData, warnings: List[str]) -> None:
        case = data.case
        fir_year: Optional[int] = None
        if case.fir_number and case.fir_number.validation_status == ValidationStatus.VALID:
            suffix = str(case.fir_number.normalized_value).split("/")[-1]
            fir_year = int(suffix) + (2000 if len(suffix) == 2 else 0)
        if fir_year and case.year and case.year.validation_status == ValidationStatus.VALID:
            if int(case.year.normalized_value) != fir_year:
                for f in (case.year, case.fir_number):
                    f.validation_status = ValidationStatus.UNCERTAIN
                    _note(f, "year field and FIR number year disagree")
                warnings.append("Year field and FIR number year disagree")
        fir_d = parse_date(str(case.fir_date.original_value)) if case.fir_date else None
        sub_d = parse_date(str(case.submission_date.original_value)) if case.submission_date else None
        if fir_d and sub_d and sub_d < fir_d:
            for f in (case.fir_date, case.submission_date):
                if f.validation_status != ValidationStatus.CONFLICTING:
                    f.validation_status = ValidationStatus.UNCERTAIN
                _note(f, "charge-sheet date is earlier than the FIR date")
            warnings.append(
                f"Charge-sheet date {sub_d.isoformat()} is earlier than the FIR date {fir_d.isoformat()} — check the source"
            )
        if fir_d and fir_year and fir_d.year != fir_year:
            if case.fir_date.validation_status == ValidationStatus.VALID:
                case.fir_date.validation_status = ValidationStatus.UNCERTAIN
            _note(case.fir_date, "FIR date year differs from FIR number year")
            warnings.append("FIR date year differs from the FIR number year")

    @staticmethod
    def _confidence(data: ChargeSheetData) -> float:
        if data.extraction_status == ExtractionStatus.FAILED:
            return 0.0
        fields = [f for f in data.all_fields() if f.original_value not in (None, "")]
        if not fields:
            return 0.0
        scores = []
        for f in fields:
            ext = f.extraction_confidence if f.extraction_confidence is not None else 0.5
            mp = f.mapping_confidence if f.mapping_confidence is not None else 0.5
            scores.append(((ext + mp) / 2) * _STATUS_WEIGHT.get(f.validation_status, 0.6))
        conf = sum(scores) / len(scores)
        if data.extraction_status == ExtractionStatus.PARTIAL:
            conf = min(conf, 0.5)
        return round(conf, 3)


def normalize_simple(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None
