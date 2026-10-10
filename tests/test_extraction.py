"""Pipeline-level behaviour: honest status, traceability, no fabrication."""

from collections import Counter
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.schemas.charge_sheet import ChargeSheetData, ExtractionStatus, ReviewStatus
from app.services import ocr_service
from app.services.extraction_service import ExtractionService


def test_scanned_without_ocr_fails_instead_of_completing(fixtures_dir: Path):
    data = ExtractionService(db=None).process(fixtures_dir / "sample_04_scanned_image.pdf")
    assert data.extraction_status == ExtractionStatus.FAILED
    assert data.overall_confidence is None
    assert not data.fields  # nothing extracted, nothing invented
    assert any(w.startswith("OCR_REQUIRED") for w in data.extraction_warnings)
    assert any(
        s.stage in ("hybrid_page_acquire", "ocr_local") and s.status.value == "failed"
        for s in data.stage_results
    )


def test_ocr_engine_error_surfaces_in_status(monkeypatch, fixtures_dir: Path):
    monkeypatch.setenv("OCR_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(ocr_service.OcrService, "is_available", lambda self: True)
    monkeypatch.setattr(
        ocr_service.OcrService,
        "recognize_png",
        lambda self, png: ocr_service.OcrResult(engine="paddleocr", error="PaddleOCR failed: kernel not supported"),
    )
    data = ExtractionService(db=None).process(fixtures_dir / "sample_04_scanned_image.pdf")
    assert data.extraction_status == ExtractionStatus.FAILED
    assert any("kernel not supported" in w for w in data.extraction_warnings)


def _mixed_pdf(path: Path) -> None:
    from PIL import Image

    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    y = 800
    for text in ["CHARGE SHEET", "District: Northtown", "Police Station: Riverside", "FIR No.: 4/2017",
                 "Acts and Sections: u/s 379 IPC", "Particulars of the Accused:", "1. John Doe s/o Richard age 30"]:
        c.drawString(50, y, text)
        y -= 18
    c.showPage()
    img = path.with_suffix(".png")
    Image.new("RGB", (400, 300), (250, 250, 250)).save(img)
    c.drawImage(ImageReader(str(img)), 0, 0, width=A4[0], height=A4[1])
    c.showPage()
    c.save()
    img.unlink()


def test_page_needing_ocr_makes_extraction_partial(tmp_path):
    pdf = tmp_path / "mixed.pdf"
    _mixed_pdf(pdf)
    data = ExtractionService(db=None).process(pdf)
    assert data.extraction_status == ExtractionStatus.PARTIAL
    assert ("FIR No.", "4/2017") in [(f.label, f.value) for f in data.fields]
    assert [p.text_source for p in data.page_summaries] == ["native", "none"]


@pytest.mark.parametrize("name", [
    "sample_01_digital_standard.pdf", "sample_02_alternate_labels.pdf", "sample_03_multipage.pdf",
    "sample_05_header_table_narrative_refs.pdf", "sample_06_missing_fields_and_traps.pdf",
])
def test_payload_traceability(fixtures_dir: Path, name):
    data = ExtractionService(db=None).process(fixtures_dir / name)
    assert isinstance(data, ChargeSheetData)
    assert data.extraction_status == ExtractionStatus.COMPLETE
    assert data.fields
    # Corrections address fields by id: ids must be unique
    dupes = [n for n, c in Counter(f.id for f in data.fields).items() if c > 1]
    assert not dupes, dupes
    for f in data.fields:
        assert f.review_status == ReviewStatus.UNREVIEWED
        assert f.page in f.pages and f.id.startswith(f"p{f.page}-")
        assert f.label or f.value
    assert data.requires_human_review


def test_fields_carry_the_page_where_their_label_is(fixtures_dir: Path):
    data = ExtractionService(db=None).process(fixtures_dir / "sample_03_multipage.pdf")
    pages = {f.label: f.page for f in data.fields}
    assert pages["District"] == 1 and pages["Accused"] == 2 and pages["Witnesses"] == 3


def test_only_labels_printed_in_the_pdf_appear(fixtures_dir: Path):
    """No fixed field list: a document without a court / charge-sheet number simply has no such field."""
    data = ExtractionService(db=None).process(fixtures_dir / "sample_06_missing_fields_and_traps.pdf")
    labels = [f.label for f in data.fields if f.label]
    assert labels == ["Name of Police Station", "FIR No. & Date", "Sections of law", "Name of IO",
                      "Accused", "Witnesses", "Brief facts"]
    by_label = {f.label: f.value for f in data.fields}
    assert by_label["FIR No. & Date"] == "19/2025 dt. 03-01-2025"  # value exactly as printed