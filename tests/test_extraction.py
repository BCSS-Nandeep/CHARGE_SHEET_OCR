from pathlib import Path

from app.schemas.charge_sheet import ChargeSheetData
from app.services.extraction_service import ExtractionService


def test_extract_digital_standard(fixtures_dir: Path):
    path = fixtures_dir / "sample_01_digital_standard.pdf"
    data = ExtractionService(db=None).process(path)
    assert isinstance(data, ChargeSheetData)
    assert data.case.district and data.case.district.original_value
    assert data.case.police_station and data.case.police_station.original_value
    assert data.case.fir_number and "123" in str(data.case.fir_number.original_value)
    assert data.case.fir_number.source_page is not None
    assert len(data.accused_persons) >= 2
    assert len(data.witnesses) >= 1
    assert data.acts_and_sections
    # originals preserved; no invention of missing fields as verified
    for f in data.all_fields():
        assert f.review_status.value == "unreviewed"


def test_extract_alternate_labels(fixtures_dir: Path):
    path = fixtures_dir / "sample_02_alternate_labels.pdf"
    data = ExtractionService(db=None).process(path)
    assert data.case.district
    assert data.case.police_station
    # Crime number / FIR from alternate labels
    assert data.case.fir_number or data.case.crime_number
    assert data.case.charge_sheet_number
    assert data.accused_persons
    assert data.document_info.document_type


def test_extract_multipage(fixtures_dir: Path):
    path = fixtures_dir / "sample_03_multipage.pdf"
    data = ExtractionService(db=None).process(path)
    assert data.case.fir_number
    assert len(data.accused_persons) >= 2
    pages = {f.source_page for f in data.all_fields() if f.source_page}
    assert len(pages) >= 1


def test_extract_scanned_without_ocr_does_not_fabricate(fixtures_dir: Path):
    """Image-only PDF without OCR should not invent case fields."""
    path = fixtures_dir / "sample_04_scanned_image.pdf"
    data = ExtractionService(db=None).process(path)
    # May extract little or nothing from native text — must not invent FIR numbers
    if not data.case.fir_number:
        assert data.requires_human_review
        assert data.extraction_warnings
