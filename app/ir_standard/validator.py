"""Turn raw VLM output into the standard IR JSON.

Every field of every section is always present. Choice values are snapped to
the canonical options of the template (misspellings like "Fe-male" or "Turnee
up" included); anything that cannot be matched is kept as ``other`` and
flagged, never silently dropped or invented. Python decides what is valid; the
VLM only proposes.
"""

from __future__ import annotations

import difflib
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from app.ir_standard.evidence import EvidenceIndex, Mark
from app.ir_standard.matching import (  # noqa: F401 (re-exported)
    _norm,
    _strip_yes,
    is_blank,
    snap_option,
    snap_relation,
)
from app.ir_standard.schema import (
    MULTI,
    SCHEMA_VERSION,
    SECTIONS,
    SINGLE,
    FieldSpec,
    SectionSpec,
    TableSpec,
)

EVIDENCE = ("tick", "bold", "yes_text", "written", "other")

# Verification statuses, worst first. A status says how well the *source* supports the VLM's answer.
STATUS_ORDER = ("conflicting", "unsupported", "missed", "uncertain", "unverified", "supported", "written", "empty")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v).strip() for v in value if str(v).strip())
    elif isinstance(value, dict):
        value = value.get("value") if "value" in value else ", ".join(str(v) for v in value.values() if v)
    s = re.sub(r"[ \t]+", " ", str(value)).strip()
    return s or None


def _evidence(value: Any) -> Optional[str]:
    if value is None:
        return None
    e = _norm(value)
    for known in EVIDENCE:
        if _norm(known) == e:
            return known
    if "tick" in e or "check" in e:
        return "tick"
    if "bold" in e:
        return "bold"
    if "yes" in e:
        return "yes_text"
    return "other"


def _flag(flags: List[dict], section: str, path: str, code: str, detail: str) -> None:
    flags.append({"section": section, "path": path, "code": code, "detail": detail})


# ---------------------------------------------------------------------------
# field normalisation
# ---------------------------------------------------------------------------

_BOX_RANK = {"ticked": 2, "empty": 1, "none": 0}


def _option_evidence(spec: FieldSpec, marks: List[Mark]) -> Dict[str, dict]:
    """Canonical option -> what the report shows for it. box / bold / yes stay separate attributes."""
    agg: Dict[str, dict] = {}
    for m in marks:
        option = snap_option(m.label, spec.options)
        if option is None:
            continue
        a = agg.setdefault(option, {"option": option, "box": "none", "bold": False, "yes": False, "at": m.at})
        if _BOX_RANK[m.box] > _BOX_RANK[a["box"]]:
            a["box"], a["at"] = m.box, m.at
        a["bold"] = a["bold"] or m.bold
        a["yes"] = a["yes"] or m.yes
    for a in agg.values():
        a["state"] = "selected" if a["box"] == "ticked" else ("uncertain" if a["bold"] or a["yes"] else "unselected")
    return agg


def _how(a: dict) -> str:
    if a["box"] == "ticked":
        return "ticked" + (", bold" if a["bold"] else "") + (", written Yes" if a["yes"] else "")
    parts = [p for p, on in (("bold", a["bold"]), ("written Yes", a["yes"])) if on]
    return (" + ".join(parts) + " only, box not ticked") if parts else "not marked"


def _public(a: dict) -> dict:
    out = {k: a[k] for k in ("option", "state", "box", "bold", "yes", "at")}
    if a.get("via"):
        out["via"] = a["via"]  # "bold": selected by bold formatting alone
    return out


def _verify_choice(
    spec: FieldSpec, chosen: List[str], claim: Optional[str], index: EvidenceIndex,
    flags: List[dict], section: str, path: str,
) -> Optional[dict]:
    """Check the model's choice against what the report marks. Never changes the choice."""
    marks = index.marks_for(spec)
    if marks is None:
        if not chosen:
            return None
        _flag(flags, section, path, "evidence_unverified",
              f"{spec.label}: {', '.join(chosen)!r} was returned but this field is not in the report text "
              "(no options, marks or written answer found); left empty")
        return {"source": index.source, "status": "unverified", "chosen": [], "candidates": [],
                "unsupported_options": list(chosen)}

    agg = _option_evidence(spec, marks)
    # Many reports mark the choice by bold alone (box left empty). When no option of the field is ticked and the
    # bold marks are unambiguous (one bold option; any number for an "any of" field) the bold option IS the
    # selection. Bold next to other marks, and a written Yes on its own, stay weak and are flagged.
    if not any(a["box"] == "ticked" for a in agg.values()):
        bolds = [a for a in agg.values() if a["bold"]]
        if (spec.kind == SINGLE and len(bolds) == 1) or (spec.kind == MULTI and bolds):
            for a in bolds:
                a["state"], a["via"] = "selected", "bold"
    positives = [a for a in agg.values() if a["state"] != "unselected"]
    nothing = {"box": "none", "bold": False, "yes": False, "state": "unselected", "at": None}
    chosen_ev = [agg.get(c) or {"option": c, **nothing} for c in chosen]
    found: List[str] = []

    def desc(items: List[dict]) -> str:
        return "; ".join(f"{a['option']} ({_how(a)})" for a in items) or "no option"

    for a in chosen_ev:
        if a["state"] == "unselected":
            found.append("unsupported")
            _flag(flags, section, path, "unsupported_selection",
                  f"{spec.label}: {a['option']!r} was returned but the report does not mark it, so it was left empty. "
                  f"The report marks: {desc(positives)}")
        elif a["state"] == "uncertain":
            found.append("uncertain")
            _flag(flags, section, path, "weak_evidence",
                  f"{spec.label}: {a['option']!r} is marked only by {_how(a)}; bold or a written Yes alone is not a ticked box")
    ticked = [a for a in positives if a["state"] == "selected"]
    if spec.kind == SINGLE:
        if len(ticked) > 1:
            found.append("conflicting")
            _flag(flags, section, path, "multiple_selected",
                  f"{spec.label}: the report ticks more than one option ({desc(ticked)}); not resolved")
        elif len(positives) > 1:
            found.append("conflicting")
            _flag(flags, section, path, "conflicting_evidence",
                  f"{spec.label}: the report marks several options differently ({desc(positives)}); not resolved")
    missed = [a for a in (positives if not chosen else ticked) if a["option"] not in chosen]
    if missed and (spec.kind == MULTI or not chosen):
        found.append("missed")
        _flag(flags, section, path, "missed_selection",
              f"{spec.label}: the report marks {desc(missed)} but it was not returned")
    if claim in ("tick", "bold", "yes_text") and chosen_ev and all(a["state"] != "unselected" for a in chosen_ev):
        shown = {k for a in chosen_ev for k, on in (("tick", a["box"] == "ticked"), ("bold", a["bold"]),
                                                    ("yes_text", a["yes"])) if on}
        if claim not in shown:
            found.append("uncertain")
            _flag(flags, section, path, "evidence_mismatch",
                  f"{spec.label}: the model reported evidence {claim!r} but the report shows {', '.join(sorted(shown))}")
    status = next((s for s in STATUS_ORDER if s in found), "supported" if chosen else "empty")
    if not chosen and not positives:
        status = "empty"
    return {
        "source": index.source,
        "status": status,
        "chosen": [_public(a) for a in chosen_ev],
        "candidates": [_public(a) for a in positives],
        "unsupported_options": [a["option"] for a in chosen_ev if a["state"] == "unselected"],
    }


def _verify_text(spec: FieldSpec, value: Optional[str], index: EvidenceIndex,
                 flags: List[dict], section: str, path: str) -> None:
    """A text value must come from the field's own column / row (a value from a neighbour is flagged)."""
    ctx = index.text_for(spec)
    if ctx is None:
        return
    if value is None:
        if not is_blank(ctx):
            _flag(flags, section, path, "missed_value", f"{spec.label}: the report has {ctx!r} but nothing was returned")
        return
    nv, nc = _norm(value), _norm(ctx)
    if not nv or not nc:
        # a dash / "N/A" normalises to nothing and would "match" any text: compare the two as blank-or-not
        if is_blank(value) == is_blank(ctx):
            return
    elif nv in nc or nc in nv or difflib.SequenceMatcher(None, nv, nc).ratio() >= 0.8:
        return
    _flag(flags, section, path, "text_not_in_source",
          f"{spec.label}: {value!r} does not match what the report prints for this field ({ctx!r})")


def _choice(spec: FieldSpec, raw: Any, flags: List[dict], section: str, path: str,
            index: Optional[EvidenceIndex] = None) -> dict:
    evidence = None
    items: List[Any]
    if isinstance(raw, dict):
        evidence = _evidence(raw.get("evidence"))
        sel = raw.get("selected", raw.get("value"))
        items = list(sel) if isinstance(sel, (list, tuple)) else ([] if sel is None else [sel])
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    elif raw is None:
        items = []
    else:
        items = [raw]
    items = [i for i in items if not is_blank(i)]

    selected: List[str] = []
    other: List[str] = []
    for item in items:
        # a multi answer may arrive as one comma-joined string
        parts = [p for p in re.split(r"\s*[;,]\s*", str(item)) if p.strip()] if spec.kind == MULTI else [str(item)]
        for part in parts:
            snapped = snap_option(part, spec.options)
            if snapped:
                if snapped not in selected:
                    selected.append(snapped)
            else:
                other.append(part.strip())

    if other:
        _flag(flags, section, path, "unrecognized_option",
              f"{spec.label}: {', '.join(other)!r} matches no option of the standard template")
    if spec.kind == SINGLE and len(selected) > 1:
        _flag(flags, section, path, "multiple_selected",
              f"{spec.label}: more than one option marked ({', '.join(selected)}); first kept")
        selected = selected[:1]
    other_text = ", ".join(other) if other else None
    if other_text and not spec.allow_other and spec.options and "Others" not in spec.options:
        pass  # kept in ``other`` for the reviewer, flagged above
    out: Dict[str, Any] = {"evidence": evidence if (selected or other) else None, "other": other_text}
    if spec.kind == SINGLE:
        out = {"selected": selected[0] if selected else None, **out}
    else:
        out = {"selected": selected, **out}
    if index is not None:
        written = index.written_answer(spec)
        if written is not None:
            return _from_written(spec, written, selected, other, index, flags, section, path)
        verification = _verify_choice(spec, selected, evidence, index, flags, section, path)
        if verification is not None:
            out["verification"] = verification
            # An option the report does not mark is an assumption by the model: leave it empty
            # (the answer stays in verification.model_answer for the reviewer; nothing is substituted).
            drop = set(verification.get("unsupported_options") or [])
            if drop:
                verification["model_answer"] = sorted(drop)
                kept = [s for s in selected if s not in drop]
                out["selected"] = (kept[0] if kept else None) if spec.kind == SINGLE else kept
        if other_text and not index.is_grounded(other_text):
            _flag(flags, section, path, "ungrounded_value_removed",
                  f"{spec.label}: {other_text!r} is not in the report; left empty")
            out["other"], other_text = None, None
        if not out["selected"] and not out["other"]:
            out["evidence"] = None
    return out


def _from_written(spec: FieldSpec, written: str, selected: List[str], other: List[str],
                  index: EvidenceIndex, flags: List[dict], section: str, path: str) -> dict:
    """The report writes this field as plain text ("Living with parents"), not as an option list.

    The value comes from that text alone. It becomes a template option only when the text itself names
    one ("Un married" -> Unmarried); otherwise it is kept as written and no option is assumed.
    """
    model = sorted(selected) + [o for o in other]
    value: Dict[str, Any] = {"selected": [] if spec.kind == MULTI else None, "evidence": None, "other": None}
    if is_blank(written):
        status = "empty"
    else:
        parts = [p for p in re.split(r"\s*[;,]\s*", written) if p.strip()] if spec.kind == MULTI else [written]
        snapped, unsnapped = [], []
        for p in parts:
            o = snap_option(p, spec.options)
            (snapped if o else unsnapped).append(o or p.strip())
        snapped = list(dict.fromkeys(snapped))
        value["selected"] = snapped if spec.kind == MULTI else (snapped[0] if snapped else None)
        value["other"] = ", ".join(unsnapped) or None
        value["evidence"] = "written"
        status = "written"
        if unsnapped:
            _flag(flags, section, path, "written_value_not_an_option",
                  f"{spec.label}: the report writes {', '.join(unsnapped)!r}, which is not one of the template's "
                  "options; kept as written and no option was assumed")
    shown = [value["selected"]] if spec.kind == SINGLE and value["selected"] else (value["selected"] or [])
    if model and sorted(map(str, model)) != sorted(map(str, shown)) and not (
            value["other"] and not shown and len(model) == 1 and _norm(model[0]) == _norm(value["other"])):
        _flag(flags, section, path, "choice_not_in_source",
              f"{spec.label}: the model returned {', '.join(map(str, model))!r} but the report writes "
              f"{written!r}; the report's own text was used")
    value["verification"] = {"source": index.source, "status": status, "chosen": [], "candidates": [],
                             "written_text": written, "model_answer": model}
    return value


def _field(spec: FieldSpec, raw: Any, flags: List[dict], section: str, path: str,
           index: Optional[EvidenceIndex] = None, ground_only: bool = False) -> Any:
    if spec.kind == "text":
        value = _text(raw)
        if index is not None and value is not None and not index.is_grounded(value):
            # not found in the report: leave the field empty rather than keep an invented value
            _flag(flags, section, path, "ungrounded_value_removed",
                  f"{spec.label}: {value[:80]!r} is not in the report; left empty")
            value = None
        if index is not None and not ground_only:
            _verify_text(spec, value, index, flags, section, path)
        return value
    return _choice(spec, raw, flags, section, path, None if ground_only else index)


def _empty_field(spec: FieldSpec) -> Any:
    if spec.kind == "text":
        return None
    return {"selected": [] if spec.kind == MULTI else None, "evidence": None, "other": None}


def _row(table: TableSpec, raw: Any, flags: List[dict], section: str, path: str,
         index: Optional[EvidenceIndex] = None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    # accept either keys or printed labels from the model
    lookup = {_norm(k): v for k, v in raw.items()}
    row: Dict[str, Any] = {}
    for col in table.columns:
        value = raw.get(col.key, lookup.get(_norm(col.key), lookup.get(_norm(col.label))))
        # table cells are only checked for being in the report (choice cells are not verified yet)
        row[col.key] = _field(col, value, flags, section, f"{path}.{col.key}", index, ground_only=True)
    return row


def _row_empty(table: TableSpec, row: dict, skip: Iterable[str] = ()) -> bool:
    for col in table.columns:
        if col.key in skip:
            continue
        v = row[col.key]
        if col.kind == "text":
            if not is_blank(v):
                return False
        elif v["selected"] or v["other"]:
            return False
    return True


def _table(table: TableSpec, raw: Any, flags: List[dict], section: str,
           index: Optional[EvidenceIndex] = None) -> List[dict]:
    rows_in: List[Any] = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    if table.fixed_rows:
        first = table.columns[0].key
        by_label: Dict[str, dict] = {}
        extras: List[tuple] = []  # rows printed under a relation the template has no row for (or repeated)
        for r in rows_in:
            if not isinstance(r, dict):
                continue
            label_text = _text(r.get(first) or next(iter(r.values()), None))
            canon = snap_relation(label_text, table.fixed_rows)  # exact: "Father's Sister" is not "Father"
            if canon and canon not in by_label:
                by_label[canon] = r
            elif label_text:
                extras.append((canon or label_text, r))
        out: List[dict] = []
        for i, label in enumerate(table.fixed_rows):
            row = _row(table, by_label.get(label, {}), flags, section, f"{section}.{table.key}.{i}", index)
            row[first] = label
            out.append(row)
        for label_text, r in extras[: table.max_rows]:
            if index is not None and not index.is_grounded(label_text):
                continue
            row_flags: List[dict] = []
            row = _row(table, r, row_flags, section, f"{section}.{table.key}.{len(out)}", index)
            row[first] = label_text  # kept as printed: no relation is assumed for it
            if _row_empty(table, row, skip=(first,)):
                continue
            flags.extend(row_flags)
            out.append(row)
        return out
    out = []
    removed = 0
    for r in rows_in[: table.max_rows]:
        row_flags: List[dict] = []
        row = _row(table, r, row_flags, section, f"{section}.{table.key}.{len(out)}", index)
        if _row_empty(table, row):
            # a row the report does not contain (or whose every value was not in the report)
            removed += 1 if any(f["code"] == "ungrounded_value_removed" for f in row_flags) else 0
            continue
        flags.extend(row_flags)
        out.append(row)
    if removed:
        _flag(flags, section, f"{section}.{table.key}", "ungrounded_rows_removed",
              f"{table.title}: {removed} row(s) returned by the model contain nothing from the report and were left out")
    # a table with no data stays in the template as one blank row (every column present, all empty)
    return out or [blank_row(table)]


def blank_row(table: TableSpec) -> dict:
    return {c.key: _empty_field(c) for c in table.columns}


# ---------------------------------------------------------------------------
# consistency checks (flags only; values are never rewritten)
# ---------------------------------------------------------------------------

_DATE_FORMATS = ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%y", "%d/%m/%y", "%Y-%m-%d")


def _parse_date(text: str) -> Optional[datetime]:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    return None


def _check_section(spec: SectionSpec, data: Dict[str, Any], flags: List[dict]) -> None:
    sec = spec.key
    for f in spec.fields:
        v = data.get(f.key)
        if f.required and (is_blank(v) if f.kind == "text" else not (v["selected"] or v["other"])):
            _flag(flags, sec, f"{sec}.{f.key}", "missing_required", f"{f.label} is required but was not found")
    if sec == "s01_individual_particulars":
        age = data.get("age")
        if not is_blank(age):
            m = re.search(r"\d+", age)
            if not m or not 10 <= int(m.group()) <= 99:
                _flag(flags, sec, f"{sec}.age", "implausible_value", f"Age {age!r} is not a plausible age")
    if sec == "s02_unique_identifiers":
        aad = data.get("aadhaar_number")
        if not is_blank(aad) and len(re.sub(r"\D", "", aad)) != 12:
            _flag(flags, sec, f"{sec}.aadhaar_number", "invalid_format", "Aadhaar number is not 12 digits")
        mob = data.get("mobile_number")
        if not is_blank(mob):
            numbers = re.findall(r"\d[\d\s-]{6,}\d", mob)
            if any(len(re.sub(r"\D", "", n)) != 10 for n in numbers):
                _flag(flags, sec, f"{sec}.mobile_number", "invalid_format", "Mobile number is not 10 digits")
    if sec == "s16_arrest_particulars":
        d = data.get("date_of_arrest")
        if not is_blank(d) and _parse_date(re.sub(r"[^\d./-]", "", d)) is None:
            _flag(flags, sec, f"{sec}.date_of_arrest", "invalid_format", f"Date of arrest {d!r} is not a date")


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def empty_section(spec: SectionSpec) -> Dict[str, Any]:
    out: Dict[str, Any] = {f.key: _empty_field(f) for f in spec.fields}
    for t in spec.tables:
        out[t.key] = _table(t, [], [], spec.key)
    return out


def validate_section(spec: SectionSpec, raw: Optional[dict], flags: List[dict],
                     index: Optional[EvidenceIndex] = None) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    lookup = {_norm(k): v for k, v in raw.items()}
    out: Dict[str, Any] = {}
    for f in spec.fields:
        value = raw.get(f.key, lookup.get(_norm(f.key), lookup.get(_norm(f.label))))
        out[f.key] = _field(f, value, flags, spec.key, f"{spec.key}.{f.key}", index)
    for t in spec.tables:
        value = raw.get(t.key, lookup.get(_norm(t.key)))
        out[t.key] = _table(t, value, flags, spec.key, index)
    _check_section(spec, out, flags)
    return out


def build_standard(
    raw_sections: Dict[str, Optional[dict]],
    not_found: Iterable[str] = (),
    indexes: Optional[Dict[str, EvidenceIndex]] = None,
) -> Dict[str, Any]:
    """Standard IR JSON from per-section model output.

    ``raw_sections`` maps section keys to what the model returned (missing keys
    become empty sections). ``not_found`` lists sections the report did not
    contain at all; they are flagged so a reviewer knows it is not a read error.
    ``indexes`` maps section keys to the evidence found in the report; when given, every
    choice and text value is checked against it (see ``_verify_choice``).
    """
    flags: List[dict] = []
    sections: Dict[str, Any] = {}
    for spec in SECTIONS:
        sections[spec.key] = validate_section(
            spec, raw_sections.get(spec.key), flags, (indexes or {}).get(spec.key))
    for key in not_found:
        _flag(flags, key, key, "section_not_found", "This section was not found in the uploaded report")
    return {"schema_version": SCHEMA_VERSION, "sections": sections, "review_flags": flags}


def blank_standard() -> Dict[str, Any]:
    """The standard IR with every field empty (what the blank template shows)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "sections": {s.key: empty_section(s) for s in SECTIONS},
        "review_flags": [],
    }
