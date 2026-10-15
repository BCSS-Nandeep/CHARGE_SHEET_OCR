"""Label → canonical field mapping (synonyms, word boundaries, hints)."""

import pytest

from app.services.field_mapping_service import best_canonical_match, is_generic_date_label, list_heading_kind


@pytest.mark.parametrize(
    "label,canonical",
    [
        ("P.S.", "police_station"), ("Distt.", "district"), ("F.I.R. Number", "fir_number"),
        ("Challan No.", "charge_sheet_number"), ("Name of IO", "io_name"), ("Name and Rank of I.O.,", "io_name"),
        ("Charge Sheet Dt.", "submission_date"), ("Charge Sheet /Final\nReport", "final_report_type"),
        ("If Supplementary /\nOriginal", "supplementary_or_original"), ("Sections of Law & Act", "acts_and_sections"),
        ("Act. I.P.C.,", "acts_and_sections"), ("Year of FIR", "year"), ("Date of registration", "fir_date"),
    ],
)
def test_synonym_mapping_variants(label, canonical):
    assert best_canonical_match(label)[0] == canonical


def test_occurrence_date_is_not_fir_date():
    assert best_canonical_match("Date of occurrence")[0] != "fir_date"


def test_generic_date_labels_and_list_headings():
    assert is_generic_date_label("Dated:") and is_generic_date_label("Dt.") and not is_generic_date_label("Date of FIR")
    assert list_heading_kind("Name of accused\nperson(s) Charge\nsheeted.") == "accused_charged"
    assert list_heading_kind("Particularsoftheaccusednot\nchargesheeted:") == "accused_not_charged"
    assert list_heading_kind("List of witnesses to be examined.") == "witnesses"
