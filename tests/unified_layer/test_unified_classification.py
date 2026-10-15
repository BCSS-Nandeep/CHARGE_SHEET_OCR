"""Document-type classification: file typing, signals, and the rule that nothing uncertain is routed silently."""

from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

from unified import CHARGE_SHEET, INTERROGATION_REPORT, UNKNOWN
from unified import classification as C
from tests.unified_layer.helpers import CS_TEXT, FIR_TEXT, RED, VAGUE_TEXT, ir_docx, make_pdf, make_png

IR_TEXT = ("Interrogation Report  CCTNS  Individual Particulars  Unique Identifiers  Aadhaar  Modus Operandi  "
           "Arrest Particulars  Family History  Previous Cases Involved  Physical Features")


def run(coro):
    return asyncio.run(coro)


def pdf_bytes(tmp_path, text):
    """A text-layer PDF whose text wraps inside the page (a single long line would run off it and be clipped)."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(40, 40, 555, 800), text, fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


def classify(data, name="x.pdf", probe=None):
    return run(C.classify(data, name, None, probe=probe))


# ------------------------------------------------------------------ file types (by content, not by name)
def test_file_kind_comes_from_content_not_name(tmp_path):
    assert C.sniff_kind(pdf_bytes(tmp_path, "hello " * 30)) == "pdf"
    assert C.sniff_kind(ir_docx(tmp_path)) == "docx"
    png = make_png(RED)
    assert C.sniff_kind(png) == "png"
    assert C.sniff_kind(b"\xff\xd8\xff\xe0" + b"0" * 20) == "jpeg"
    assert C.sniff_kind(b"II*\x00" + b"0" * 20) == "tiff"
    assert C.sniff_kind(b"MM\x00*" + b"0" * 20) == "tiff"
    # a PDF named .txt is still a PDF; a text file named .pdf is not
    assert C.check_file(pdf_bytes(tmp_path, "hello " * 30), "report.txt") == "pdf"
    with pytest.raises(C.UnsupportedFile):
        C.check_file(b"just some text, not a pdf", "report.pdf")


def test_unsupported_files_are_refused_with_a_useful_message():
    with pytest.raises(C.UnsupportedFile, match="empty"):
        C.check_file(b"", "a.pdf")
    with pytest.raises(C.UnsupportedFile, match="Legacy Word"):
        C.check_file(b"\xd0\xcf\x11\xe0" + b"0" * 50, "old.doc")
    with pytest.raises(C.UnsupportedFile, match="macOS"):
        C.check_file(b"\x00\x05\x16\x07" + b"0" * 50, "._real.pdf")
    other_zip = io.BytesIO()
    with zipfile.ZipFile(other_zip, "w") as z:
        z.writestr("notes.txt", "x")
    with pytest.raises(C.UnsupportedFile, match="PDF, DOCX, PNG, JPEG or TIFF"):
        C.check_file(other_zip.getvalue(), "archive.docx")  # a ZIP named .docx without word/document.xml


def test_service_filename_adds_the_real_extension_only_when_needed():
    assert C.service_filename("report.pdf", "pdf") == "report.pdf"
    assert C.service_filename("Report.PDF", "pdf") == "Report.PDF"
    assert C.service_filename("scan.jpeg", "jpeg") == "scan.jpeg"
    assert C.service_filename("scan", "tiff") == "scan.tif"
    assert C.service_filename("C:\\x\\y\\file.bin", "docx") == "file.bin.docx"


# ------------------------------------------------------------------------------------- decisions
def test_interrogation_report_text_is_routed_automatically(tmp_path):
    c = classify(pdf_bytes(tmp_path, IR_TEXT))
    assert (c.document_type, c.needs_manual, c.decision) == (INTERROGATION_REPORT, False, "auto")
    assert c.signals[INTERROGATION_REPORT]["markers"]["title_region"]


def test_charge_sheet_text_is_routed_automatically(tmp_path):
    c = classify(pdf_bytes(tmp_path, CS_TEXT))
    assert (c.document_type, c.needs_manual) == (CHARGE_SHEET, False)


def test_text_without_spaces_still_matches_the_defining_phrases(tmp_path):
    c = classify(pdf_bytes(tmp_path, "CHARGESHEET (Undersec.,173Cr.P.C.,) FinalReport/ChargesheetNo. Actsandsections " * 2))
    assert c.document_type == CHARGE_SHEET


def test_fir_is_not_silently_sent_to_the_charge_sheet_extractor(tmp_path):
    """A FIR shares words with a charge sheet ('section 173', 'FIR', 'accused'); it must wait for a human decision."""
    c = classify(pdf_bytes(tmp_path, FIR_TEXT + " complainant accused informant brief facts investigation"))
    assert c.needs_manual and c.document_type == UNKNOWN
    assert "First Information Report" in " ".join(c.reasons)


def test_unrelated_document_needs_manual_choice(tmp_path):
    c = classify(pdf_bytes(tmp_path, VAGUE_TEXT))
    assert c.needs_manual and c.document_type == UNKNOWN and c.decision == "manual_required"


def test_detector_hints_alone_never_reach_automatic_routing(tmp_path):
    """Words the detectors count ('accused', 'investigation', 'complainant', 'brief facts') appear in many documents."""
    c = classify(pdf_bytes(tmp_path, "brief facts of the investigation: the complainant named the accused " * 6))
    assert c.needs_manual
    assert c.signals[CHARGE_SHEET]["score"] <= C.WEAK_CAP


def test_both_forms_present_without_a_clear_winner_needs_manual_choice(tmp_path):
    c = classify(pdf_bytes(tmp_path, IR_TEXT + " " + CS_TEXT))
    assert c.needs_manual and c.document_type == UNKNOWN
    assert "both forms are plausible" in " ".join(c.reasons)


def test_scanned_document_without_any_text_needs_manual_choice(tmp_path):
    c = classify(make_pdf(tmp_path / "scan.pdf", images=[make_png(RED)], text="").read_bytes())
    assert c.needs_manual and "no readable text" in " ".join(c.reasons)


def test_image_without_probe_text_needs_manual_choice():
    c = classify(make_png(RED), "photo.png")
    assert c.needs_manual and c.facts["kind"] == "png"


def test_probe_text_from_ocr_can_route_a_scanned_file(tmp_path):
    """The worker's probe supplies OCR text (and the charge-sheet detector's verdict) for files without a text layer."""
    async def probe(data, name):
        return {"text": CS_TEXT, "text_source": "ocr", "text_chars": len(CS_TEXT), "pages_examined": 2, "page_count": 4,
                "ocr_requested": True, "ocr_available": True, "ocr_error": None,
                "detector": {"document_type": "charge_sheet", "confidence": 0.9, "section_hits": [], "uncertain": False, "notes": ""}}

    c = classify(make_pdf(tmp_path / "scan.pdf", images=[make_png(RED)], text="").read_bytes(), probe=probe)
    assert c.document_type == CHARGE_SHEET and c.text["source"] == "ocr" and not c.needs_manual


def test_unavailable_worker_is_reported_and_text_layer_is_still_used(tmp_path):
    async def probe(data, name):
        return None

    c = classify(pdf_bytes(tmp_path, IR_TEXT), probe=probe)
    assert c.document_type == INTERROGATION_REPORT
    assert any("not available" in w for w in c.warnings)


def test_extension_mismatch_is_noted_but_content_wins(tmp_path):
    c = classify(pdf_bytes(tmp_path, IR_TEXT), "report.docx")
    assert c.facts["kind"] == "pdf" and not c.facts["extension_matches_content"]
    assert any("extension" in w for w in c.warnings)


def test_classification_never_keeps_document_text(tmp_path):
    c = classify(pdf_bytes(tmp_path, IR_TEXT))
    blob = str(c.as_dict())
    assert "Modus Operandi  Arrest Particulars" not in blob  # no excerpt of the text is carried into the record


def test_decide_rule_directly():
    # (ir, cs, ir_strong, cs_strong, has_text)
    assert C.decide(0.9, 0.1, True, False, True, 0.55, 0.25)[:2] == (INTERROGATION_REPORT, False)
    assert C.decide(0.9, 0.7, True, True, True, 0.55, 0.25)[:2] == (UNKNOWN, True)     # too close
    assert C.decide(0.9, 0.7, True, True, True, 0.55, 0.15)[:2] == (INTERROGATION_REPORT, False)
    assert C.decide(0.5, 0.0, True, False, True, 0.55, 0.25)[:2] == (UNKNOWN, True)    # below the threshold
    assert C.decide(0.99, 0.0, False, False, True, 0.55, 0.25)[:2] == (UNKNOWN, True)  # no defining phrase
    assert C.decide(0.9, 0.0, True, False, False, 0.55, 0.25)[:2] == (UNKNOWN, True)   # nothing readable
