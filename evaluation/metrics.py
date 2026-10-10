"""Metric computation: extraction output vs. ground truth.

Every metric is a count — numerator and denominator are always reported, so
"1.000" can be read as "3 of 3", not as a general accuracy claim.

Definitions
-----------
classification      correct document type / documents
case_fields         (correct values + correctly-absent fields) / ground-truth case fields.
                    A field expected to be blank counts as wrong if a valid value is produced.
case_precision      correct values / values produced for ground-truth fields
case_recall         correct values / non-blank ground-truth values
<kind>_precision    matched records / extracted records    (kind = accused, witnesses, ios)
<kind>_recall       matched records / ground-truth records
attributes          correct attributes / attributes checked on matched records
names_exact         exactly spelled names / matched records
legal_precision     correct (act, section) pairs / extracted pairs
legal_recall        correct (act, section) pairs / ground-truth pairs
related_refs_*      precision / recall of other-case FIR numbers found in the narrative
conflict_precision  expected conflicts found / conflicts reported
conflict_recall     expected conflicts found / ground-truth conflicts
completeness        ground-truth items extracted correctly / ground-truth items
ocr_lines           reference lines recognised with similarity >= 0.9 / reference lines
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from app.schemas.charge_sheet import ChargeSheetData, ValidationStatus
from app.services.text_normalize import compact
from app.services.value_parsers import digits, parse_date

NAME_MATCH = 0.8
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

    def c(self, name: str) -> Count:
        return self.counts.setdefault(name, Count())

    def miss(self, text: str) -> None:
        self.mismatches.append(text)


def _sim(a: str, b: str) -> float:
    a, b = compact(a), compact(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _strip_relation(value: str) -> str:
    return re.sub(r"(?i)^\s*(?:[sdwch]\s*/\s*[o.]|son of|daughter of|wife of|husband of)\s*", "", value or "")


def case_value_match(key: str, expected: str, got: str) -> bool:
    if key in {"fir_date", "submission_date"}:
        e, g = parse_date(expected), parse_date(got)
        return e is not None and e == g
    if key in {"fir_number", "crime_number", "charge_sheet_number"}:
        return re.sub(r"\s", "", expected.lower()) == re.sub(r"\s", "", got.lower())
    if key == "year":
        return digits(expected) == digits(got)
    return compact(expected) == compact(got)


def _act_key(act: Optional[str]) -> str:
    return compact(act or "").replace("act", "")


def _section_key(section: Optional[str]) -> str:
    return re.sub(r"\s", "", (section or "").lower())


def _greedy_match(expected: List[dict], extracted: List[str]) -> List[Tuple[int, int]]:
    pairs = []
    for ei, e in enumerate(expected):
        for xi, x in enumerate(extracted):
            s = _sim(e.get("name", ""), x)
            if s >= NAME_MATCH:
                pairs.append((s, ei, xi))
    pairs.sort(reverse=True)
    used_e, used_x, out = set(), set(), []
    for _, ei, xi in pairs:
        if ei in used_e or xi in used_x:
            continue
        used_e.add(ei)
        used_x.add(xi)
        out.append((ei, xi))
    return out


def _fv(f) -> Optional[str]:
    return None if f is None or f.original_value in (None, "") else str(f.original_value)


def _attribute_ok(attr: str, expected: str, person) -> Tuple[bool, Optional[str]]:
    if attr == "role_contains":
        got = _fv(getattr(person, "witness_type", None)) or _fv(getattr(person, "role", None)) or ""
        return compact(expected) in compact(got), got
    if attr == "address_contains":
        got = _fv(person.address) or ""
        return compact(expected) in compact(got), got
    got = _fv(getattr(person, attr, None))
    if got is None:
        return False, None
    if attr == "parent_or_spouse":
        return compact(_strip_relation(expected)) == compact(_strip_relation(got)), got
    if attr in {"contact", "identification"}:
        return digits(expected) == digits(got), got
    if attr == "serial_no":
        return digits(expected).lstrip("0") == digits(got).lstrip("0"), got
    return compact(expected) == compact(got), got


def _persons(ev: DocEvaluation, kind: str, expected: List[dict], extracted: List, label: str, show: bool) -> None:
    names = [_fv(p.name) or "" for p in extracted]
    matches = _greedy_match(expected, names)
    prec, rec = ev.c(f"{kind}_precision"), ev.c(f"{kind}_recall")
    prec.num += len(matches)
    prec.den += len(names)
    rec.num += len(matches)
    rec.den += len(expected)
    matched_e = {ei for ei, _ in matches}
    for ei, e in enumerate(expected):
        if ei not in matched_e:
            ev.miss(f"{label}: missing {e.get('name') if show else f'#{ei}'}")
    matched_x = {xi for _, xi in matches}
    for xi, n in enumerate(names):
        if xi not in matched_x:
            ev.miss(f"{label}: unexpected extra record {n if show else f'#{xi}'}")
    for ei, xi in matches:
        e, person = expected[ei], extracted[xi]
        ev.c("names_exact").add(compact(e["name"]) == compact(names[xi]))
        if compact(e["name"]) != compact(names[xi]):
            ev.miss(f"{label}[{ei}].name: expected {e['name']!r} got {names[xi]!r}" if show else f"{label}[{ei}].name differs")
        for attr, value in e.items():
            if attr in {"name", "note"}:
                continue
            ok, got = _attribute_ok(attr, value, person)
            ev.c("attributes").add(ok)
            if not ok:
                ev.miss(f"{label}[{ei}].{attr}: expected {value!r} got {got!r}" if show else f"{label}[{ei}].{attr} wrong")


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

    # Classification
    got_type = _fv(data.document_info.document_type) if data.document_info else None
    ev.c("classification").add(got_type == exp.get("document_type"))

    # Case fields
    for key, expected in (exp.get("case") or {}).items():
        f = getattr(data.case, key, None)
        got = _fv(f)
        acc = ev.c("case_fields")
        if expected is None:
            produced_valid = got is not None and f.validation_status not in (ValidationStatus.INVALID, ValidationStatus.MISSING)
            acc.add(not produced_valid)
            if produced_valid:
                ev.c("case_precision").add(False)
                ev.miss(f"case.{key}: expected blank, got {got!r}" if show else f"case.{key}: spurious value")
            continue
        rec = ev.c("case_recall")
        if got is None:
            acc.add(False)
            rec.add(False)
            ev.miss(f"case.{key}: missing (expected {expected!r})" if show else f"case.{key}: missing")
            continue
        ok = case_value_match(key, expected, got)
        acc.add(ok)
        rec.add(ok)
        ev.c("case_precision").add(ok)
        if not ok:
            ev.miss(f"case.{key}: expected {expected!r} got {got!r}" if show else f"case.{key}: wrong value")

    # Persons
    _persons(ev, "accused", exp.get("accused") or [], data.accused_persons, "accused", show)
    _persons(ev, "witnesses", exp.get("witnesses") or [], data.witnesses, "witness", show)
    _persons(ev, "ios", exp.get("investigating_officers") or [], data.investigating_officers, "io", show)

    # Legal sections
    want = {(_act_key(s["act"]), _section_key(s["section"])) for s in exp.get("legal_sections") or []}
    got_pairs = {
        (_act_key(_fv(s.act)), _section_key(_fv(s.section))) for s in data.acts_and_sections if s.section
    }
    ev.c("legal_precision").num += len(want & got_pairs)
    ev.c("legal_precision").den += len(got_pairs)
    ev.c("legal_recall").num += len(want & got_pairs)
    ev.c("legal_recall").den += len(want)
    for a, s in sorted(want - got_pairs):
        ev.miss(f"legal: missing {a} {s}")
    for a, s in sorted(got_pairs - want):
        ev.miss(f"legal: unexpected {a} {s}")

    # Related case references
    want_refs = {re.sub(r"\s", "", r) for r in exp.get("related_case_references") or []}
    got_refs = {re.sub(r"\s", "", _fv(r) or "") for r in data.related_case_references}
    ev.c("related_refs_precision").num += len(want_refs & got_refs)
    ev.c("related_refs_precision").den += len(got_refs)
    ev.c("related_refs_recall").num += len(want_refs & got_refs)
    ev.c("related_refs_recall").den += len(want_refs)
    for r in sorted(got_refs - want_refs):
        ev.miss(f"related_refs: unexpected {r}")
    for r in sorted(want_refs - got_refs):
        ev.miss(f"related_refs: missing {r}")

    # Conflicts: same field and the same set of values
    expected_conflicts = exp.get("conflicts") or []
    detected = [(c.field_name, {compact(v) for v in c.values}) for c in data.source_conflicts]
    found_idx = set()
    for ec in expected_conflicts:
        vals = {compact(v) for v in ec["values"]}
        hit = next((i for i, (fname, dv) in enumerate(detected) if fname == ec["field"] and dv == vals and i not in found_idx), None)
        ev.c("conflict_recall").add(hit is not None)
        if hit is not None:
            found_idx.add(hit)
        else:
            ev.miss(f"conflict missed: {ec['field']}" + (f" {ec['values']}" if show else ""))
    for i, (fname, dv) in enumerate(detected):
        ok = i in found_idx
        ev.c("conflict_precision").add(ok)
        if not ok:
            ev.miss(f"conflict not in ground truth: {fname}" + (f" {sorted(dv)}" if show else ""))

    # Completeness: every ground-truth item that should be present
    comp = ev.c("completeness")
    comp.num += ev.c("case_recall").num + ev.c("legal_recall").num
    comp.den += ev.c("case_recall").den + ev.c("legal_recall").den
    for kind in ("accused", "witnesses", "ios"):
        comp.num += ev.c(f"{kind}_recall").num
        comp.den += ev.c(f"{kind}_recall").den

    # OCR line recognition (scanned documents with reference transcriptions)
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
