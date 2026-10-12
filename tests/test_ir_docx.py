"""IR DOCX extraction — field detection on the interrogation report form."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.docx_service import DocxService
from app.services.extraction_service import ExtractionService

SAMPLE = Path(
    r"C:\Users\Dell\Downloads\IR_54_Documents_50\IR_54_Documents_50"
    r"\Cyberabad-Miyapur PS-1452-2025-Mallidi Sai Kiran reddy"
    r"\Interrogation_report-Cyberabad-Miyapur PS-1452-2025-Mallidi Sai Kiran reddy.docx"
)


pytestmark = pytest.mark.skipif(not SAMPLE.exists(), reason="sample IR DOCX not on this machine")


def test_docx_tables_extracted():
    result = DocxService().extract(SAMPLE)
    assert result.page_count == 1
    assert result.pages[0].route == "native"
    assert len(result.pages[0].tables) >= 3
    assert "Interrogation" in result.pages[0].text or "Mallidi" in result.pages[0].text


def test_ir_fields_and_json_shape():
    data = ExtractionService().process(SAMPLE)
    assert data.schema_version == "2.0.0"
    assert data.document_info.document_type == "interrogation_report"
    assert data.extraction_status.value == "complete"
    assert len(data.fields) >= 40

    def first(label: str):
        needle = label.lower()
        for f in data.fields:
            if (f.label or "").lower() == needle:
                return f
        return None

    name = first("Name")
    assert name is not None and "mallidi" in (name.value or "").lower()
    mobile = first("Mobile Number")
    assert mobile is not None and "8106521319" in (mobile.value or "")
    aadhaar = first("Aadhar Number") or first("Aadhaar Number")
    assert aadhaar is not None and "7642" in (aadhaar.value or "")
    sex = first("Sex")
    assert sex is not None and (sex.value or "").lower() == "male"
    assert first("Living Status") is not None

    payload = data.model_dump()
    assert "fields" in payload
    assert payload["fields"][0].keys() >= {"id", "page", "label", "value", "source", "review_status"}
