"""Gate 0/1 page router decisions."""

from app.services.page_router import (
    PageSignals,
    accused_numbering_gaps,
    build_signals,
    decide_route,
    need_docling_assist,
    ocr_accept,
)


def test_zero_chars_forces_ocr():
    s = build_signals(
        page_number=1, width=600, height=800, rotation=0, text="",
        image_count=1, image_coverage=0.95, table_count=0,
    )
    d = decide_route(s)
    assert d.route == "ocr"
    assert any("char_count=0" in r for r in d.reasons)


def test_short_labeled_page_stays_native():
    text = "Particulars of the Accused:\nA1 John Doe s/o Richard age 30"
    s = build_signals(
        page_number=2, width=600, height=800, rotation=0, text=text,
        image_count=0, image_coverage=0.0, table_count=0,
    )
    assert s.char_count < 80 and s.label_hits >= 1
    assert decide_route(s).route == "native"


def test_glued_text_routes_both():
    glued = "INTHECOURTOFHONBLEJUDGE" * 5 + " District Police Station FIR"
    s = build_signals(
        page_number=1, width=600, height=800, rotation=0, text=glued,
        image_count=0, image_coverage=0.0, table_count=1,
    )
    d = decide_route(s)
    assert d.route == "both"
    assert any("space_ratio" in r or "avg_word_len" in r for r in d.reasons)


def test_clean_digital_is_native():
    text = (
        "01 District : Sample\n02 Police Station : Central\n"
        "FIR No. 83/2020\nActs and Sections : 379 IPC\n"
        "Name(s) of the accused A1\n"
    )
    s = build_signals(
        page_number=1, width=600, height=800, rotation=0, text=text * 3,
        image_count=0, image_coverage=0.0, table_count=2,
    )
    d = decide_route(s)
    assert d.route == "native"


def test_docling_only_on_digital_forms():
    s = PageSignals(
        page_index=1, width=600, height=800, table_count=2, label_hits=6, empty_cell_ratio=0.4
    )
    assert need_docling_assist(s, "native", enabled=True) is True
    assert need_docling_assist(s, "ocr", enabled=True) is False
    assert need_docling_assist(s, "native", enabled=False) is False


def test_ocr_accept_gate3():
    ok, _ = ocr_accept(ocr_char_count=200, mean_confidence=0.8, low_conf_box_ratio=0.1, ocr_label_hits=2)
    assert ok is True
    ok, reasons = ocr_accept(ocr_char_count=10, mean_confidence=0.2, low_conf_box_ratio=0.9, ocr_label_hits=0)
    assert ok is False
    assert reasons


def test_accused_numbering_gaps():
    assert accused_numbering_gaps(["A1", "A2", "A4"]) == [3]
    assert accused_numbering_gaps(["A1", "A2", "A3"]) == []
