from app.services.field_mapping_service import (
    best_canonical_match,
    extract_acts_sections,
    extract_kv_candidates,
    extract_person_blocks,
)


def test_synonym_mapping_variants():
    assert best_canonical_match("P.S.")[0] == "police_station"
    assert best_canonical_match("Distt.")[0] == "district"
    assert best_canonical_match("F.I.R. Number")[0] == "fir_number"
    assert best_canonical_match("Challan No.")[0] == "charge_sheet_number"
    assert best_canonical_match("Name of IO")[0] == "io_name"


def test_kv_and_acts_extraction():
    text = """
    1. District                    : Sample District
    2. Police Station              : Central PS
    4. FIR No.                     : 123/2024
    Acts and Sections           : u/s 302, 34 IPC
    """
    cands = extract_kv_candidates(text, 1)
    names = {c["canonical"] for c in cands if c["canonical"]}
    assert "district" in names
    assert "police_station" in names
    assert "fir_number" in names
    acts = extract_acts_sections(text, 1)
    assert acts
    assert "302" in acts[0]["section"]


def test_person_blocks():
    text = """
    Particulars of the Accused:
    1. Suresh Singh s/o Mohan Singh age 32 r/o Ward 4
    2. Vijay Patel s/o K. Patel age 28 r/o Market Road
    List of Witnesses:
    1. PW-1 Ramesh Kumar (complainant)
    Brief Facts:
    Something happened.
    """
    accused = extract_person_blocks(text, 1, "accused")
    witnesses = extract_person_blocks(text, 1, "witness")
    assert len(accused) >= 2
    assert "Suresh" in accused[0]["name"]
    assert len(witnesses) >= 1
