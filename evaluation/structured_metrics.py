"""Field-level accuracy of the structured result against verified ground truth.

Every metric is a count (numerator / denominator). "Exact match" is strict: the machine value
(never a human correction) must equal the ground-truth value after the comparison rule for its
kind — spacing / case / punctuation ignored for text and identifiers, calendar-date equality for
dates, digits equal for ages and phone numbers. A conflicting value never counts as correct.

Reported separately, because they are different failures:

case_exact            expected case values whose machine value matches exactly / non-blank expected
case_missing          ...no value found (not_found / explicit N/A)
case_wrong            ...a value that differs (includes conflicting / unverified-but-different)
case_fabricated       ground-truth-blank fields given a value *presented as extracted* / ground-truth-blank fields
case_fabricated_flagged  ...given a value that is flagged unverified / ambiguous / illegible (a reviewer sees it
                      is unsupported) / ground-truth-blank fields
critical_exact        the same as case_exact restricted to identifiers, dates and year (FIR no., charge
                      sheet no., FIR date, submission date, year)
<kind>_names          expected accused / witnesses / officers found by exact name / expected
<kind>_names_loose    ...found with the expected name *inside* a record's name field (it may carry extra text such
                      as "s/o <parent>") / expected — shows a person was found even if the field is not clean
<kind>_spurious       extracted records whose name is in no expected entry / extracted records
<kind>_attrs          attributes of matched people that match exactly / expected attributes
legal_sections        expected (act, section) pairs found as act and section in one record / expected
legal_sections_loose  ...act and section number both present in one record's act + section text, however the
                      model split them between the two fields / expected
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.schemas.charge_sheet import ChargeSheetData
from app.schemas.structured import StructuredValue, ValueStatus
from app.services.text_normalize import compact
from app.services.value_parsers import digits, parse_date
from evaluation.metrics import Count

CRITICAL_CASE = {"fir_number", "crime_number", "charge_sheet_number", "fir_date", "submission_date", "year"}
DATE_KEYS = {"fir_date", "submission_date"}
ID_KEYS = {"fir_number", "crime_number", "charge_sheet_number"}
PERSON_ATTRS = ("parent_or_spouse", "age", "occupation", "identification", "contact", "serial_no", "gender")


@dataclass
class StructEval:
    document_id: str
    private: bool
    counts: Dict[str, Count] = field(default_factory=dict)
    mismatches: List[str] = field(default_factory=list)
    statuses: Dict[str, int] = field(default_factory=dict)

    def c(self, name: str) -> Count:
        return self.counts.setdefault(name, Count())

    def miss(self, text: str) -> None:
        self.mismatches.append(text)


def machine_value(v: Optional[StructuredValue]) -> Optional[str]:
    """The machine's value (a human correction is not an extraction result)."""
    if v is None or v.status in (ValueStatus.NOT_FOUND, ValueStatus.CONFLICTING):
        return None
    return v.value


def same(kind: str, key: str, expected: str, got: Optional[str]) -> bool:
    if got is None:
        return False
    if key in DATE_KEYS:
        e = parse_date(expected)
        return e is not None and e == parse_date(got)
    if key in ("age", "contact"):
        return digits(expected) == digits(got) and bool(digits(expected))
    if key == "year":
        return digits(expected) == digits(got)
    if key in ID_KEYS:
        return re.sub(r"\s", "", expected.lower()) == re.sub(r"\s", "", got.lower())
    return compact(expected) == compact(got)


def _section_numbers(text: str) -> set:
    return {t for t in re.split(r"[^0-9A-Za-z()]+", (text or "").lower()) if t}


def evaluate_structured(data: ChargeSheetData, exp: dict, *, document_id: str, private: bool) -> StructEval:
    show = not private
    ev = StructEval(document_id, private)
    s = data.structured
    if s is None:
        ev.miss("no structured result")
        return ev
    for _, v in s.iter_values():
        ev.statuses[v.status.value] = ev.statuses.get(v.status.value, 0) + 1

    # ---- case-level values
    for key, expected in (exp.get("case") or {}).items():
        v = s.single["case"].get(key)
        got = machine_value(v)
        if expected is None:
            as_extracted = v is not None and v.status in (ValueStatus.EXTRACTED, ValueStatus.EXPLICIT_NA)
            ev.c("case_fabricated").add(as_extracted)
            ev.c("case_fabricated_flagged").add(got is not None and not as_extracted)
            if got is not None:
                how = "presented as extracted" if as_extracted else f"flagged {v.status.value}"
                ev.miss(f"case.{key}: expected blank but a value was given, {how}" + (f" ({got!r})" if show else ""))
            continue
        ok = same("case", key, expected, got)
        ev.c("case_exact").add(ok)
        if key in CRITICAL_CASE:
            ev.c("critical_exact").add(ok)
        if not ok:
            kind = "conflicting" if v and v.status == ValueStatus.CONFLICTING else ("missing" if got is None else "wrong")
            ev.c("case_missing" if kind != "wrong" else "case_wrong").num += 1
            ev.miss(f"case.{key}: {kind}" + (f" (expected {expected!r}, got {got!r})" if show else ""))
    for name in ("case_missing", "case_wrong"):
        ev.c(name).den = ev.c("case_exact").den

    # ---- people
    for kind, section in (("accused", "accused"), ("witnesses", "witnesses"), ("investigating_officers", "investigating_officers")):
        expected = [p for p in (exp.get(kind) or []) if p.get("name")]
        records = s.lists.get(section, [])
        names = [machine_value(r.fields.get("name")) for r in records]
        used: set = set()
        for person in expected:  # loose: the expected name is inside some record's name field
            ev.c(f"{kind}_names_loose").add(any(n and compact(person["name"]) in compact(n) for n in names))
        for i, person in enumerate(expected):
            idx = next((j for j, n in enumerate(names) if j not in used and n and compact(n) == compact(person["name"])), None)
            ev.c(f"{kind}_names").add(idx is not None)
            if idx is None:
                ev.miss(f"{kind}[{i}]: name not found exactly" + (f" ({person['name']!r})" if show else ""))
                continue
            used.add(idx)
            rec = records[idx]
            for attr in PERSON_ATTRS:
                want = person.get(attr)
                if want in (None, ""):
                    continue
                got = machine_value(rec.fields.get(attr))
                ok = same(kind, attr, str(want), got)
                ev.c(f"{kind}_attrs").add(ok)
                if not ok:
                    ev.miss(f"{kind}[{i}].{attr}: " + ("missing" if got is None else "wrong")
                            + (f" (expected {want!r}, got {got!r})" if show else ""))
            if person.get("address_contains"):
                got = machine_value(rec.fields.get("address")) or ""
                ok = compact(person["address_contains"]) in compact(got)
                ev.c(f"{kind}_attrs").add(ok)
                if not ok:
                    ev.miss(f"{kind}[{i}].address: does not contain the expected text")
        named = [n for n in names if n]
        ev.c(f"{kind}_spurious").num += sum(1 for j, n in enumerate(names) if n and j not in used)
        ev.c(f"{kind}_spurious").den += len(named)
        # records with no name at all cannot be matched; count them as spurious too
        nameless = sum(1 for n in names if not n)
        ev.c(f"{kind}_spurious").num += nameless
        ev.c(f"{kind}_spurious").den += nameless

    # ---- legal sections: act and section number must be found together in one record
    for item in exp.get("legal_sections") or []:
        act, sec = compact(item.get("act") or "").replace("act", ""), str(item.get("section") or "").lower()
        found = False
        for rec in s.lists.get("acts_and_sections", []):
            ra = compact(machine_value(rec.fields.get("act")) or "").replace("act", "")
            rs = _section_numbers(machine_value(rec.fields.get("section")) or "")
            if ra == act and sec in rs:
                found = True
                break
        ev.c("legal_sections").add(found)
        # loose: act name and section number both appear in one record's act + section text,
        # even when the model split them across the two fields differently
        loose = any(
            act in compact(((machine_value(r.fields.get("act")) or "") + " " + (machine_value(r.fields.get("section")) or "")))
            .replace("act", "")
            and sec in _section_numbers((machine_value(r.fields.get("act")) or "") + " " + (machine_value(r.fields.get("section")) or ""))
            for r in s.lists.get("acts_and_sections", [])
        )
        ev.c("legal_sections_loose").add(loose)
        if not found:
            ev.miss(f"legal: {item.get('act')} {item.get('section')} not found as one act/section record")

    # ---- honesty checks: not a measure of correctness, but of evidence
    values = [v for _, v in s.iter_values() if v.status != ValueStatus.NOT_FOUND]
    ev.c("values_with_evidence").num += sum(1 for v in values if v.status == ValueStatus.EXTRACTED and v.source_text)
    ev.c("values_with_evidence").den += len(values)
    return ev


def aggregate(evals: List[StructEval]) -> Dict[str, Count]:
    total: Dict[str, Count] = {}
    for ev in evals:
        for name, count in ev.counts.items():
            total.setdefault(name, Count()).merge(count)
    return total
