"""OCR availability and error reporting (no OCR engine is run here)."""

import io
import os

import pytest
from PIL import Image

from app.services import ocr_service
from app.services.ocr_service import OcrService


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_model_hoster_connectivity_probe_disabled():
    assert os.environ.get("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK") == "True"


def test_pytesseract_without_binary_is_not_an_engine(monkeypatch):
    monkeypatch.setattr(ocr_service.shutil, "which", lambda _: None)
    monkeypatch.setattr(ocr_service, "_WINDOWS_TESSERACT", ocr_service.Path("Z:/nonexistent/tesseract.exe"))
    assert ocr_service.tesseract_binary() is None
    assert ocr_service.tesseract_available() is False


def test_engine_failure_is_reported_not_returned_as_empty_text(monkeypatch):
    monkeypatch.setenv("OCR_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()

    class Broken:
        def predict(self, _):
            raise RuntimeError("kernel not supported")

    monkeypatch.setattr(OcrService, "_get_paddle", lambda self: Broken())
    monkeypatch.setattr(ocr_service, "tesseract_binary", lambda: None)
    res = OcrService().recognize_png(_png())
    assert res.lines == []
    assert res.error and "PaddleOCR failed" in res.error


def test_disabled_ocr_reports_reason(monkeypatch):
    monkeypatch.setenv("OCR_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    res = OcrService().recognize_png(_png())
    assert res.error and "disabled" in res.error.lower()
    assert OcrService().is_available() is False


def test_parse_paddle_v3_and_v2_shapes_keep_boxes():
    v3 = [{"rec_texts": ["DISTRICT", ""], "rec_scores": [0.99, 0.1], "rec_boxes": [[10, 20, 110, 50], [0, 0, 1, 1]]}]
    lines = OcrService._parse_paddle_result(v3)
    assert [(l.text, l.bbox) for l in lines] == [("DISTRICT", (10.0, 20.0, 110.0, 50.0))]
    v2 = [[[[[5, 5], [50, 5], [50, 25], [5, 25]], ("FIR No.", 0.95)]]]
    lines = OcrService._parse_paddle_result(v2)
    assert lines[0].text == "FIR No." and lines[0].bbox == (5.0, 5.0, 50.0, 25.0)


@pytest.mark.ocr
@pytest.mark.skipif(os.environ.get("RUN_OCR_TESTS") != "1", reason="set RUN_OCR_TESTS=1 to run local OCR")
def test_real_ocr_on_scanned_fixture(monkeypatch, fixtures_dir):
    monkeypatch.setenv("OCR_ENABLED", "true")
    from app.config import get_settings
    from app.services.extraction_service import ExtractionService

    get_settings.cache_clear()
    data = ExtractionService(db=None).process(fixtures_dir / "sample_04_scanned_image.pdf")
    assert data.extraction_status.value == "complete"
    assert data.case.fir_number and data.case.fir_number.original_value == "901/2021"
    assert data.page_summaries[0].text_source == "ocr"
