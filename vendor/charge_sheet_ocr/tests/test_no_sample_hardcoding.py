"""Runtime code must not contain sample-specific values or depend on file names.

Forbidden tokens are derived automatically from every ground-truth file
(synthetic, and real documents when the private fixtures are present), so a
new evaluation document extends the check without editing this test.
The whole source text is scanned, comments included: example values from
confidential documents must not leak into the code base either.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from evaluation.ground_truth import load_ground_truth

APP = Path(__file__).resolve().parents[1] / "app"

# General charge-sheet vocabulary that legitimately appears in parsing rules.
GENERIC_WORDS = {
    "sample", "district", "police", "station", "court", "magistrate", "judicial", "first", "class",
    "special", "sessions", "charge", "sheet", "original", "supplementary", "final", "report", "type",
    "ndps", "excise", "labor", "labour", "driver", "mechanic", "painter", "student", "business",
    "panch", "panchanama", "witness", "assisted", "owner", "land", "complainant", "unknown", "person",
    "investigating", "officer", "conducted", "inspector", "constable", "market", "road", "ward",
    "colony", "village", "mandal", "block", "east", "north", "zone", "bank", "manager", "local",
    "shopkeeper", "city", "canal", "lakeside", "theft", "eyewitness", "agril", "with", "from",
    "circle", "jmfc", "thana", "riverside", "view", "hill", "coastal", "harbour", "central", "accused",
}
_HARDCODE_FREE_FILES = sorted(p for p in APP.rglob("*.py"))


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in {"note", "field", "ocr_reference_lines"}:
                continue
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def forbidden_tokens():
    tokens = set()
    for gt in load_ground_truth(include_private=True):
        expected = dict(gt.expected)
        expected.pop("document_type", None)
        for s in _strings(expected):
            # Whole identifiers / dates (e.g. "310/2024", "14.07.2024")
            for m in re.findall(r"\d{1,6}/\d{2,4}|\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}|\d{9,12}", s):
                tokens.add(m)
            for word in re.findall(r"[A-Za-z]{4,}", s):
                w = word.lower()
                if w not in GENERIC_WORDS:
                    tokens.add(w)
    return tokens


def test_ground_truth_loaded():
    assert load_ground_truth(include_private=False), "public ground truth fixtures missing"


def test_no_ground_truth_values_in_runtime_code():
    tokens = forbidden_tokens()
    hits = []
    for path in _HARDCODE_FREE_FILES:
        text = path.read_text(encoding="utf-8").lower()
        for tok in tokens:
            pattern = rf"(?<![a-z0-9]){re.escape(tok.lower())}(?![a-z0-9])"
            if re.search(pattern, text):
                hits.append(f"{path.relative_to(APP.parent)}: {tok!r}")
    assert not hits, "Sample-specific values found in runtime code:\n" + "\n".join(sorted(hits))


def test_runtime_code_never_reads_ground_truth_or_uploads_by_name():
    for path in _HARDCODE_FREE_FILES:
        text = path.read_text(encoding="utf-8")
        assert "ground_truth" not in text, f"{path.name} references ground truth"
        assert "evaluation" not in re.findall(r"^\s*(?:from|import)\s+(\w+)", text, re.M), path.name
        assert not re.search(r"[0-9a-f]{32}_", text), f"{path.name} references a stored upload name"


def test_extraction_independent_of_filename(tmp_path, fixtures_dir):
    from app.services.extraction_service import ExtractionService

    src = fixtures_dir / "sample_05_header_table_narrative_refs.pdf"
    renamed = tmp_path / "completely_different_name.pdf"
    shutil.copy(src, renamed)
    svc = ExtractionService(db=None)
    a = svc.process(src).model_dump(exclude={"extracted_at"})
    b = svc.process(renamed).model_dump(exclude={"extracted_at"})
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


@pytest.mark.parametrize(
    "label",
    ["His song, Age 19 years cast", "Occ", "kumar S/o Ashok, age", "Sri Banoth Ramesh, S/o Dhup Singh age"],
)
def test_short_synonyms_do_not_hijack_person_lines(label):
    from app.services.field_mapping_service import best_canonical_match

    assert best_canonical_match(label)[0] is None
