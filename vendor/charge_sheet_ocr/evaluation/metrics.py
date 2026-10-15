"""Metric computation: per-document field list (schema v2) vs. ground truth.

Every metric is a count — numerator and denominator are always reported, so
"1.000" can be read as "3 of 3", not as a general accuracy claim.

The result has no fixed field list: each field is a label and a value as
printed in the PDF. The ground-truth files (written for the earlier fixed
schema, and left unchanged) are therefore used as lists of values that must
appear in the extracted fields. Labels are not scored — a label is correct by
construction when it is the PDF's own text, and the ground truth records no
labels. Expected-blank fields cannot be scored for the same reason.

Definitions
-----------
classification      correct document type / documents
case_values         expected case values found in a field value / non-blank expected case values
case_values_form    expected case values found in a form field (not the running narrative) /
                    non-blank expected case values — shows labels and values were paired, not just read
names               expected accused / witness / IO names found in a form field / expected names
legal_sections      expected (act, section) pairs found together in one field value / expected pairs
completeness        expected items (case values, names, sections) found in any field / expected items
labelled_fields     fields that have a label / fields  (descriptive only — not accuracy)
ocr_lines           reference lines recognised with similarity >= 0.9 / reference lines
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional

from app.schemas.charge_sheet import ChargeSheetData
from app.services.text_normalize import compact
from app.services.value_parsers import all_dates, digits, parse_date

OCR_LINE_MATCH = 0.9


@dataclass
class Count:
    num: int = 0
    den: int = 0

    def add(self, ok: bool) -> None:
        self.num += int(ok)
        self.den += 1

    def merge(self, other: "Count") -> None:
        self.num += other.num
        self.den += other.den

    @property
    def value(self) -> Optional[float]:
        return round(self.num / self.den, 3) if self.den else None

    def fmt(self) -> str:
        return "n/a (0/0)" if not self.den else f"{self.num / self.den:.3f} ({self.num}/{self.den})"


@dataclass
class DocEvaluation:
    document_id: str
    layout: str
    private: bool
    extraction_status: str = ""
    counts: Dict[str, Count] = field(default_factory=dict)
    mismatches: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)  # informational, not failures

    def c(self, name: str) -> Count:
        return self.counts.setdefault(name, Count())

    def miss(self, text: str) -> None:
        self.mismatches.append(text)



def value_found(key: str, expected: str, text: str) -> bool:
    """Whether a ground-truth value appears in an extracted value (as printed, spacing ignored)."""
    if not text:
        return False
    if key in {"fir_date", "submission_date"}:
        e = parse_date(expected)
        return e is not None and e in all_dates(text)
    if key in {"fir_number", "crime_number", "charge_sheet_number"}:
        return re.sub(r"\s", "", expected.lower()) in re.sub(r"\s", "", text.lower())
    if key == "year":
        return digits(expected) in digits(text)
    return compact(expected) in compact(text)


def _act_key(act: Optional[str]) -> str:
    return compact(act or "").replace("act", "")


def evaluate_document(
    data: ChargeSheetData,
    gt_expected: dict,
    *,
    document_id: str,
    layout: str,
    private: bool,
    page_texts: Optional[Dict[int, str]] = None,
) -> DocEvaluation:
    show = not private  # never echo real personal data into shared reports
    ev = DocEvaluation(document_id, layout, private, data.extraction_status.value)
    exp = gt_expected
    values = [f.corrected_value or f.value for f in data.fields]
    form_values = [f.corrected_value or f.value for f in data.fields if not f.text_block]
    every = values + [f.label or "" for f in data.fields]

    ev.c("classification").add((data.document_info.document_type if data.document_info else None) == exp.get("document_type"))
    labelled = ev.c("labelled_fields")
    labelled.num += sum(1 for f in data.fields if f.label)
    labelled.den += len(data.fields)

    comp = ev.c("completeness")
    for key, expected in (exp.get("case") or {}).items():
        if expected is None:
            continue
        anywhere = any(value_found(key, expected, v) for v in every)
        in_form = any(value_found(key, expected, v) for v in form_values + [f.label or "" for f in data.fields])
        ev.c("case_values").add(anywhere)
        ev.c("case_values_form").add(in_form)
        comp.add(anywhere)
        if not anywhere:
            ev.miss(f"case.{key}: missing (expected {expected!r})" if show else f"case.{key}: missing")
        elif not in_form:
            # Printed without a label (e.g. a court heading above the narrative): found, but not as a
            # form field — counted in case_values_form, reported as a note rather than a failure
            ev.notes.append(f"case.{key}: only inside the narrative" + (f" ({expected!r})" if show else ""))

    for kind in ("accused", "witnesses", "investigating_officers"):
        for i, person in enumerate(exp.get(kind) or []):
            name = person.get("name")
            if not name:
                continue
            ok = any(compact(name) in compact(v) for v in form_values)
            ev.c("names").add(ok)
            comp.add(ok or any(compact(name) in compact(v) for v in values))
            if not ok:
                ev.miss(f"{kind}[{i}]: name not found in a form field" + (f" ({name!r})" if show else ""))

    for s in exp.get("legal_sections") or []:
        act, section = _act_key(s.get("act")), compact(s.get("section") or "")
        ok = any(section in compact(v) and act in compact(v).replace("act", "") for v in values)
        ev.c("legal_sections").add(ok)
        comp.add(ok)
        if not ok:
            ev.miss(f"legal: {s.get('act')} {s.get('section')} not found in one field")

    if page_texts is not None and exp.get("ocr_reference_lines"):
        for idx, ref in enumerate(exp["ocr_reference_lines"]):
            lines = [ln.strip() for ln in (page_texts.get(ref["page"]) or "").splitlines() if ln.strip()]
            best = max((SequenceMatcher(None, ref["text"].lower(), ln.lower()).ratio() for ln in lines), default=0.0)
            ev.c("ocr_lines").add(best >= OCR_LINE_MATCH)
            if best < OCR_LINE_MATCH:
                ev.miss(f"ocr: page {ref['page']} reference line #{idx} not recognised"
                        + (f" (best similarity {best:.2f}): {ref['text']!r}" if show else ""))
    return ev


def aggregate(evals: List[DocEvaluation]) -> Dict[str, Count]:
    total: Dict[str, Count] = {}
    for ev in evals:
        for name, count in ev.counts.items():
            total.setdefault(name, Count()).merge(count)
    return total
