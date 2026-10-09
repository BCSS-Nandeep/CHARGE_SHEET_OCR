"""Ensure runtime extraction has no FIR-83 / sample-specific hardcoding."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services.field_mapping_service import best_canonical_match
from app.services.text_normalize import soften_pdf_text

APP = Path(__file__).resolve().parents[1] / "app"

# Values that must not appear as string literals in runtime extraction code
FORBIDDEN_LITERALS = {
    "Aerrolla",
    "Errolla",
    "Rajender Goud",
    "83/2020",
    "Haveli Ghanapur",
    "Kuchanpally",
    "Alprazolam",
    "445a415f-5f82-4ce1-b847-09d6c974a473",
    "Kasa Aravind",
}


def _string_literals(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.add(node.value)
    return out


def test_no_forbidden_literals_in_runtime_services():
    hits = []
    for path in (APP / "services").rglob("*.py"):
        literals = _string_literals(path)
        for bad in FORBIDDEN_LITERALS:
            for lit in literals:
                if bad.lower() in lit.lower():
                    hits.append(f"{path.name}: contains {bad!r} in {lit!r}")
    assert not hits, "Sample-specific hardcoding found:\n" + "\n".join(hits)


def test_short_synonyms_do_not_hijack_person_lines():
    assert best_canonical_match("His song, Age 19 years cast")[0] is None
    assert best_canonical_match("Occ")[0] is None
    assert best_canonical_match("kumar S/o Ashok, age")[0] is None
    assert best_canonical_match("Sri Banoth Ramesh, S/o Dhup Singh age")[0] is None
    assert best_canonical_match("Police Station")[0] == "police_station"
    assert best_canonical_match("Year")[0] == "year"
    assert best_canonical_match("FIR No.")[0] == "fir_number"


def test_soften_glued_charge_sheet_headers():
    raw = "Dist.Medak PS:HAVELIGHANAPUR\nParticularsofthewitnessestobeexamined\nActs andsections"
    soft = soften_pdf_text(raw)
    assert "Medak" in soft
    assert "PS:" in soft or "PS :" in soft
    assert "witnesses to be examined" in soft.lower()
    assert "and sections" in soft.lower()
    # Space-aligned columns must remain splittable
    aligned = soften_pdf_text("Distt.                 Sample East")
    assert "  " in aligned
