"""Form / zone parsing: identifiers come from the form, never from narrative prose."""

from app.services.field_mapping_service import best_canonical_match
from app.services.form_parser import DocItem, parse_cell_pairs, parse_document, select_fields
from app.services.layout_service import TableRow


def row(page, y, *cells):
    return DocItem(page, y, " ".join(cells), row=TableRow(page=page, cells=list(cells), origin="pdf_table"))


def line(page, y, text):
    return DocItem(page, y, text)


def fields(items_by_page):
    parsed = parse_document(items_by_page)
    return parsed, {k: v.candidate.value for k, v in select_fields(parsed.candidates).items()}


def test_narrative_fir_numbers_and_dates_never_become_case_fields():
    page1 = [
        row(1, 10, "01", "Dist. Northtown", "PS: RIVERSIDE", "FIR. No. 4/2017", "On 02.03.2017 at 1500 hrs."),
        row(1, 30, "02", "Charge sheet No.", "/2017"),
        row(1, 50, "03", "Date", "05-06-2017"),
    ]
    page2 = [
        line(2, 10, "Hon'ble Sir,"),
        line(2, 20, "The accused was also involved in Cr. No: 99/2016 of another PS dated 01.01.2016."),
        line(2, 30, "Basing on the panchanama a case was registered in Cr. No. 4/2017 u/s 379 IPC."),
    ]
    parsed, f = fields([page1, page2])
    assert f["fir_number"] == "4/2017"
    assert "crime_number" not in f
    assert f["fir_date"].startswith("02.03.2017")
    assert f["submission_date"] == "05-06-2017"  # generic 'Date' after the charge-sheet number
    refs = {(r.number, r.registration) for r in parsed.case_references}
    assert ("99/2016", False) in refs and ("4/2017", True) in refs


def test_generic_date_label_resolves_from_preceding_field():
    pairs, _ = parse_cell_pairs("Year: 2017, FIR. No. 4/2017.\nDt.02.03.2017.")
    assert [(c, v) for _, v, c, _ in pairs][:2] == [("year", "2017"), ("fir_number", "4/2017.")]
    page = [
        row(1, 10, "01", "District: Northtown.\nPolice Station: Riverside.", "Year: 2017, FIR. No. 4/2017.\nDt.02.03.2017."),
        row(1, 30, "02", "Charge-sheet No.", "03. Dated: 05.06.2017"),
    ]
    _, f = fields([page])
    assert f["fir_date"] == "02.03.2017."
    assert f["submission_date"] == "05.06.2017"
    assert "charge_sheet_number" not in f  # blank in the source, not filled with the date


def test_conflicting_form_values_are_preserved_not_silently_chosen():
    page = [
        line(1, 10, "District: Northtown"),
        line(1, 20, "Police Station: Riverside"),
        line(1, 30, "Distt.: Southtown"),
    ]
    parsed = parse_document([page])
    sel = select_fields(parsed.candidates)["district"]
    assert sel.conflict is not None
    assert set(sel.conflict.values) == {"Northtown", "Southtown"}
    assert [a.value for a in sel.alternates]


def test_compound_fir_number_and_date_label():
    _, f = fields([[line(1, 10, "FIR No. & Date : 4/2017 dt. 02-03-2017")]])
    assert f["fir_number"] == "4/2017"
    assert f["fir_date"] == "02-03-2017"


def test_document_title_is_not_a_field_label():
    page = [
        line(1, 5, "CHARGE SHEET"),
        line(1, 6, "(Under sec. 173 Cr.P.C.)"),
        row(1, 10, "01", "District: Northtown"),
    ]
    _, f = fields([page])
    assert "charge_sheet_number" not in f


def test_witness_table_continues_on_next_page_and_ends_at_form_row():
    page1 = [
        row(1, 10, "14", "List of witnesses to be examined.", "Mentioned below"),
        row(1, 20, "01", "Sri. A. One S/o B, age: 30", "Eye witness"),
        row(1, 30, "02", "Sri. C. Two S/o D, age: 40", "Panch"),
    ]
    page2 = [
        row(2, 10, "03", "Sri. E. Three, SI of Police", "IO"),
        row(2, 20, "15", "Final Report is False, action taken u/s 182/211 IPC", ""),
        line(2, 40, "Hon'ble Sir,"),
    ]
    parsed = parse_document([page1, page2])
    items = parsed.lists["witnesses"]
    assert [i.serial for i in items] == ["1", "2", "3"]
    assert items[2].page == 2 and items[2].role == "IO"


def test_free_text_after_table_does_not_extend_witness_list():
    page = [
        row(1, 10, "14", "Particulars of the witnesses", ""),
        row(1, 20, "01", "Sri. A. One S/o B, age: 30", "Eye witness"),
        line(1, 40, "IN THE COURT OF THE JUDICIAL MAGISTRATE AT NORTHTOWN"),
        line(1, 50, "Honoured Sir,"),
    ]
    parsed = parse_document([page])
    assert len(parsed.lists["witnesses"]) == 1
    assert "COURT" not in parsed.lists["witnesses"][0].text


def test_glued_labels_map_by_meaning():
    assert best_canonical_match("NameoftheI.Os.,Includingrankand\nnumber")[0] == "io_name"
    assert best_canonical_match("FinalReport/ChargesheetNo.")[0] == "charge_sheet_number"
    assert best_canonical_match("Typeoffinalreport")[0] == "final_report_type"
    # Recognised non-schema labels stay unmapped instead of being forced into a field
    assert best_canonical_match("Dateonwhichthecomplainantwas informedaboutthefinalresult", include_hints=True)[0].startswith("hint:")
    assert best_canonical_match(
        "Details of property recovered/seized during the course of investigation and relied upon."
    )[0] == "seized_property"
