"""Field list: the PDF's own labels and values, in reading order, nothing renamed (no real document data)."""

from app.services.field_builder import build_fields
from app.services.field_mapping_service import best_canonical_match
from app.services.form_parser import DocItem, drop_page_furniture, parse_cell_pairs
from app.services.layout_service import HeaderPair, TableRow


def row(page, y, *cells):
    return DocItem(page, y, " ".join(cells), row=TableRow(page=page, cells=list(cells), origin="pdf_table"))


def line(page, y, text):
    return DocItem(page, y, text)


def header(page, label, value, order):
    return DocItem(page, 0.0, f"{label}: {value}", header=HeaderPair(label, value, page, order))


def pairs(fields):
    return [(f.label, f.value) for f in fields]


def test_header_table_and_form_rows_keep_their_printed_labels():
    page = [
        header(1, "DISTRICT", "NORTHTOWN", 0), header(1, "FIR. No.", "4/2017", 1),
        row(1, 10, "02", "Charge Sheet No.", "/2017."),
        row(1, 20, "03", "Charge Sheet /Final\nReport", "Charge Sheet"),
    ]
    fields = build_fields([page])
    assert pairs(fields) == [
        ("DISTRICT", "NORTHTOWN"), ("FIR. No.", "4/2017"),
        ("Charge Sheet No.", "/2017."), ("Charge Sheet /Final Report", "Charge Sheet"),
    ]
    assert [f.serial for f in fields] == [None, None, "02", "03"]
    assert [f.source for f in fields] == ["header_table", "header_table", "form_row", "form_row"]


def test_cells_holding_their_own_pairs_become_separate_fields():
    page = [
        row(1, 10, "01", "Dist. Northtown", "PS: RIVERSIDE", "FIR. No. 4/2017", "On 02.03.2017 at 1500 hrs."),
        row(1, 20, "01", "District: Northtown. Police Station: Riverside.", "Year: 2017"),
        row(1, 30, "02", "Charge-sheet No.", "03. Dated: 05.06.2017"),
    ]
    assert pairs(build_fields([page])) == [
        ("Dist.", "Northtown"), ("PS", "RIVERSIDE"), ("FIR. No.", "4/2017"), ("On", "02.03.2017 at 1500 hrs."),
        ("District", "Northtown."), ("Police Station", "Riverside."), ("Year", "2017"),
        ("Charge-sheet No.", ""), ("Dated", "05.06.2017"),
    ]


def test_narrative_numbers_and_colons_stay_inside_the_narrative():
    page1 = [row(1, 10, "01", "FIR. No.", "4/2017")]
    page2 = [
        line(2, 10, "Hon'ble Sir,"),
        line(2, 20, "The accused was also involved in Cr. No: 99/2016 of another PS dated 01.01.2016."),
        line(2, 30, "Age: 30 years, he was found with the stolen bicycle near the market."),
    ]
    fields = build_fields([page1, page2])
    assert pairs(fields)[0] == ("FIR. No.", "4/2017")
    assert len(fields) == 2 and fields[1].label is None  # the narrative has no label in this document
    assert "Cr. No: 99/2016" in fields[1].value and "Age: 30 years" in fields[1].value


def test_labelled_narrative_continues_across_pages():
    fields = build_fields([
        [row(1, 10, "17", "Brief facts of the case")],
        [line(2, 10, "Honoured Sir,"), line(2, 20, "Facts of the case are that the complainant reported a theft.")],
        [line(3, 10, "Hence the charge.")],
    ])
    assert len(fields) == 1
    f = fields[0]
    assert (f.label, f.serial, f.pages) == ("Brief facts of the case", "17", [1, 2, 3])
    assert f.value.startswith("Honoured Sir,") and f.value.endswith("Hence the charge.")


def test_same_label_twice_keeps_both_values():
    fields = build_fields([[line(1, 10, "District: Northtown"), line(1, 20, "Distt.: Southtown")]])
    assert pairs(fields) == [("District", "Northtown"), ("Distt.", "Southtown")]


def test_compound_label_value_kept_as_printed():
    assert pairs(build_fields([[line(1, 10, "FIR No. & Date : 4/2017 dt. 02-03-2017")]])) == [
        ("FIR No. & Date", "4/2017 dt. 02-03-2017")
    ]


def test_text_before_any_label_is_kept_without_a_label():
    fields = build_fields([[line(1, 5, "POLICE FINAL REPORT"), line(1, 10, "District: Northtown")]])
    assert pairs(fields) == [(None, "POLICE FINAL REPORT"), ("District", "Northtown")]


def test_witness_table_continues_on_next_page_and_ends_at_next_form_row():
    page1 = [
        row(1, 10, "14", "List of witnesses to be examined.", "Mentioned below"),
        row(1, 20, "01", "Sri. A. One S/o B, age: 30", "Eye witness"),
        row(1, 30, "02", "Sri. C. Two S/o D, age: 40", "Panch"),
    ]
    page2 = [
        row(2, 10, "03", "Sri. E. Three, SI of Police", "IO"),
        row(2, 20, "15", "Final Report is False, action taken u/s 182/211 IPC", "Nil"),
    ]
    fields = build_fields([page1, page2])
    assert [f.label for f in fields] == ["List of witnesses to be examined.", "Final Report is False, action taken u/s 182/211 IPC"]
    witnesses = fields[0]
    assert witnesses.value.split("\n") == [
        "Mentioned below", "01 | Sri. A. One S/o B, age: 30 | Eye witness", "02 | Sri. C. Two S/o D, age: 40 | Panch",
        "03 | Sri. E. Three, SI of Police | IO",
    ]
    assert witnesses.pages == [1, 2] and fields[1].serial == "15" and fields[1].value == "Nil"


def test_free_text_after_a_table_list_is_not_a_list_entry():
    fields = build_fields([[
        row(1, 10, "14", "Particulars of the witnesses", ""),
        row(1, 20, "01", "Sri. A. One S/o B, age: 30", "Eye witness"),
        line(1, 40, "IN THE COURT OF THE JUDICIAL MAGISTRATE AT NORTHTOWN"),
    ]])
    assert "COURT" not in fields[0].value
    assert fields[1].label is None and fields[1].value.startswith("IN THE COURT OF")


def test_ocr_specks_are_dropped():
    fields = build_fields([[line(1, 5, "District: Northtown"), line(1, 6, "N"), line(1, 7, "'0")]])
    assert pairs(fields) == [("District", "Northtown")]


def test_field_ids_are_page_and_position():
    fields = build_fields([[line(1, 5, "District: A"), line(1, 6, "Year: 2020")], [line(2, 5, "Court: B")]])
    assert [f.id for f in fields] == ["p1-01", "p1-02", "p2-01"]


def test_pairs_inside_a_cell_continue_over_lines():
    assert parse_cell_pairs("FIR. No. 185/2021\nOn 01.10.2021 at 1745\nhrs.") == (
        [("FIR. No.", "185/2021"), ("On", "01.10.2021 at 1745\nhrs.")], ""
    )
    # 'U/s' inside a value is not a label
    assert parse_cell_pairs("Sections 188 IPC & U/s 8(C) r/w 22 NDPS Act") == (
        [("Sections", "188 IPC & U/s 8(C) r/w 22 NDPS Act")], ""
    )


def test_glued_labels_are_recognised():
    assert best_canonical_match("NameoftheI.Os.,Includingrankand\nnumber")[0] == "io_name"
    assert best_canonical_match("FinalReport/ChargesheetNo.")[0] == "charge_sheet_number"
    assert best_canonical_match("Typeoffinalreport")[0] == "final_report_type"


def test_scanner_watermarks_and_repeated_footers_are_dropped_rows_kept():
    pages = [
        [row(1, 10, "01", "District", "Northtown"), line(1, 900, "Scanned with CamScanner")],
        [line(2, 5, "Office of the SHO Riverside"), line(2, 50, "Facts of the case are that ..."),
         line(2, 900, "Scanned with Cam Scanner")],
        [line(3, 5, "Office of the SHO Riverside"), line(3, 50, "Hence the charge.")],
    ]
    kept, dropped = drop_page_furniture(pages)
    texts = [it.text for page in kept for it in page]
    assert dropped == 4
    assert texts == ["01 District Northtown", "Facts of the case are that ...", "Hence the charge."]


def test_narrative_lines_join_into_paragraphs_without_changing_words():
    from app.services.text_normalize import join_narrative_lines

    full = "x" * 70
    lines = ["IN THE COURT OF THE MAGISTRATE", "Honoured Sir,", f"Facts {full}", f"more {full}", "ends here.",
             f"Next {full}", f"para {full}", "Hence the charge."]
    text = join_narrative_lines(lines)
    assert text.split("\n") == [
        "IN THE COURT OF THE MAGISTRATE", "Honoured Sir,", f"Facts {full} more {full} ends here.", "",
        f"Next {full} para {full} Hence the charge.",
    ]


def test_court_heading_above_an_unlabelled_narrative_becomes_its_label():
    fields = build_fields([
        [row(1, 10, "14", "Particulars of the witnesses", ""), row(1, 20, "01", "Sri. A. One S/o B", "Panch")],
        [line(2, 10, "IN THE HON'BLE JUDICIAL MAGISTRATE COURT"), line(2, 20, "AT NORTHTOWN."),
         line(2, 30, "Honoured Sir,"), line(2, 40, "Facts of the case are that the complainant reported a theft.")],
    ])
    assert [f.label for f in fields] == ["Particulars of the witnesses",
                                         "IN THE HON'BLE JUDICIAL MAGISTRATE COURT AT NORTHTOWN."]
    assert "COURT" not in fields[0].value
    assert fields[1].value.startswith("Honoured Sir,") and fields[1].text_block


def test_separator_columns_are_left_out_of_list_rows():
    fields = build_fields([[row(1, 10, "14", "Particulars of the witnesses", ""),
                            row(1, 20, "01", "Sri. A. One S/o B", ":", "Panch")]])
    assert fields[0].value == "01 | Sri. A. One S/o B | Panch"
