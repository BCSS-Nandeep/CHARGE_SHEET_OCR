"""Regression tests for FIR 83/2020 charge sheet (uploaded sample)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.extraction_service import ExtractionService

FIR_PDF = (
    Path(__file__).resolve().parents[1]
    / "uploads"
    / "80d251b418464972b530f8b865dd58ee_2021-04-19_13.28.35.818.0.38051334.pdf"
)


pytestmark = pytest.mark.skipif(not FIR_PDF.exists(), reason="FIR 83 PDF not in uploads/")


@pytest.fixture(scope="module")
def fir_data():
    return ExtractionService(db=None).process(FIR_PDF)


def test_case_identifiers(fir_data):
    assert "Medak" in (fir_data.case.district.original_value or "")
    assert "Haveli" in (fir_data.case.police_station.original_value or "")
    assert fir_data.case.year.normalized_value == "2020" or fir_data.case.year.original_value == "2020"
    assert "83/2020" in (fir_data.case.fir_number.original_value or "")
    assert "05.05.2020" in (fir_data.case.fir_date.original_value or "")
    assert "19.04.2021" in (fir_data.case.submission_date.original_value or "")


def test_legal_sections_deduped(fir_data):
    pairs = [
        (
            (s.act.normalized_value if s.act else None),
            (s.section.normalized_value if s.section else None),
        )
        for s in fir_data.acts_and_sections
    ]
    assert ("IPC", "188") in pairs or any(p[1] == "188" for p in pairs)
    assert len(fir_data.acts_and_sections) <= 6  # no form-number explosion
    # no garbage lone digits
    for _, sec in pairs:
        assert sec not in {"04", "05", "06", "07", "08"}


def test_accused_and_conflicts(fir_data):
    assert len(fir_data.accused_persons) == 1
    a = fir_data.accused_persons[0]
    assert "Rajender" in (a.name.original_value or "")
    assert a.age and a.age.original_value == "33"
    assert "32" in (a.age.alternate_values or [])
    names = " ".join(c.field_name for c in fir_data.source_conflicts)
    assert "accused.age" in names
    assert "accused.name" in names
    # unresolved
    assert all(c.resolution == "unresolved" for c in fir_data.source_conflicts)


def test_witnesses_structured(fir_data):
    assert len(fir_data.witnesses) >= 5
    names = " ".join((w.name.original_value or "") for w in fir_data.witnesses)
    assert "Tayer" in names or "Mohammad" in names
    assert "Lingampally" in names or "Sathyanarayana" in names
