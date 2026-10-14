"""Validation of a ``StructuredData`` result.

Reports problems; never rewrites a value. A value with a format or evidence problem
keeps its text and status, gains ``needs_review`` and a note, and an issue is listed.
``is_valid`` is False only for *error*-severity issues (structural or evidence
contradictions); missing expected fields and odd formats are warnings.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from app.schemas.structured import (
    REQUIRED,
    SECTION_BY_KEY,
    SECTIONS,
    StructuredData,
    StructuredValidation,
    StructuredValue,
    ValidationIssue,
    ValueStatus,
    field_def,
)
from app.services.text_normalize import compact
from app.services.value_parsers import has_date_shape, parse_date

_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_RELATION = re.compile(r"(?i)(?<![A-Za-z])(?:[sdwcr]/o|son of|daughter of|wife of|age[d]?\b|\d+\s*(?:yrs?|years))")
_SECTION = re.compile(r"\d")
_VALID_REVIEW = {"unreviewed", "needs_review", "corrected", "verified", "rejected"}


def _flag(value: StructuredValue, note: str) -> None:
    value.needs_review = True
    value.note = f"{value.note}; {note}" if value.note and note not in value.note else (value.note or note)


def validate_structured(data: StructuredData, page_texts: Optional[Dict[int, str]] = None) -> StructuredValidation:
    issues: List[ValidationIssue] = []
    page_nums = {p.page for p in data.pages}

    def add(code: str, message: str, *, severity: str = "warning", path: Optional[str] = None, page: Optional[int] = None):
        issues.append(ValidationIssue(code=code, severity=severity, path=path, page=page, message=message))

    # ---- structure: sections and fields must be exactly the schema's
    for sec in SECTIONS:
        if sec.repeated:
            if sec.key not in data.lists:
                add("section_missing", f"section '{sec.key}' is absent from the result", severity="error", path=sec.key)
        elif sec.key not in data.single:
            add("section_missing", f"section '{sec.key}' is absent from the result", severity="error", path=sec.key)
    for key in list(data.single) + list(data.lists):
        if key not in SECTION_BY_KEY:
            add("unexpected_section", f"unexpected section '{key}'", severity="error", path=key)

    for path, v in data.iter_values():
        parts = path.split(".")
        sec_key, fkey = parts[0], parts[-1]
        fd = field_def(sec_key, fkey)
        if fd is None:
            add("unexpected_field", f"unexpected field '{fkey}' in '{sec_key}'", severity="error", path=path)
            continue
        if v.review_status not in _VALID_REVIEW:
            add("bad_review_status", f"unknown review status '{v.review_status}'", severity="error", path=path)

        # ---- source-page references and evidence
        has_value = v.value is not None and v.status != ValueStatus.NOT_FOUND
        if v.status == ValueStatus.NOT_FOUND:
            if v.value is not None or v.candidates:
                add("not_found_has_value", "status not_found but a value is present", severity="error", path=path)
            continue
        if v.status == ValueStatus.CONFLICTING:
            vals = {compact(c.value) for c in v.candidates}
            if len(vals) < 2:
                add("conflict_without_candidates", "status conflicting but fewer than two distinct candidates",
                    severity="error", path=path)
            else:
                add("conflict", f"{len(vals)} different values found on pages {v.pages}; review required", path=path)
            v.needs_review = True
            for c in v.candidates:
                _check_page(c.source_page, page_nums, path, add)
            continue
        if has_value:
            _check_page(v.source_page, page_nums, path, add)
            if v.status in (ValueStatus.EXTRACTED, ValueStatus.EXPLICIT_NA, ValueStatus.AMBIGUOUS):
                if not v.source_text:
                    add("no_evidence", "value has no source text", severity="error", path=path, page=v.source_page)
                elif compact(v.value or "") not in compact(v.source_text):
                    add("evidence_mismatch", "value is not contained in its source text", severity="error",
                        path=path, page=v.source_page)
                elif page_texts and v.source_page in page_texts and \
                        compact(v.source_text) not in compact(page_texts[v.source_page]):
                    add("evidence_not_on_page", "source text is not on the cited page", severity="error",
                        path=path, page=v.source_page)
            seen = {compact(c.value) for c in v.candidates}
            if len(seen) > 1:
                add("unused_candidates", f"{len(seen) - 1} other value(s) were seen but not used (no evidence under a "
                    "fitting label); they are listed with the value", severity="info", path=path, page=v.source_page)
            if v.status == ValueStatus.UNVERIFIED:
                add("unverified_value", "value not found in the page text; check against the document", path=path,
                    page=v.source_page)
            if v.status == ValueStatus.ILLEGIBLE:
                add("illegible_value", "value contains an unreadable-text marker", path=path, page=v.source_page)
            if v.status == ValueStatus.AMBIGUOUS:
                add("ambiguous_label", v.note or "label next to the value does not fit this field", path=path,
                    page=v.source_page)

            # ---- formats (flag only; the value is never converted)
            if v.status in (ValueStatus.EXTRACTED, ValueStatus.AMBIGUOUS, ValueStatus.UNVERIFIED):
                _check_format(fd.kind, v, path, add)

    # ---- required fields
    for spec, description in REQUIRED:
        alternatives = spec.split("|")
        if any(_present(data, a) for a in alternatives):
            continue
        add("required_missing", f"expected {description} was not found in the document", path=spec)

    # ---- duplicates
    for sec_key in ("accused", "witnesses", "investigating_officers"):
        names: Dict[str, int] = {}
        serials: Dict[str, int] = {}
        for i, rec in enumerate(data.lists.get(sec_key, [])):
            n = rec.fields.get("name")
            if n and n.value:
                k = compact(n.value)
                if k in names:
                    add("duplicate_record", f"'{sec_key}' entries {names[k]} and {i} have the same name",
                        path=f"{sec_key}.{i}.name")
                    _flag(n, "same name as another entry")
                names.setdefault(k, i)
            s = rec.fields.get("serial_no")
            if s and s.value:
                k = compact(s.value)
                if k in serials:
                    add("duplicate_serial", f"'{sec_key}' entries {serials[k]} and {i} share serial no. {s.value!r}",
                        path=f"{sec_key}.{i}.serial_no")
                    _flag(s, "same serial number as another entry")
                serials.setdefault(k, i)
    seen_acts: Dict[str, int] = {}
    for i, rec in enumerate(data.lists.get("acts_and_sections", [])):
        a, s = rec.fields.get("act"), rec.fields.get("section")
        k = compact((a.value if a else "") or "") + "|" + compact((s.value if s else "") or "")
        if k != "|" and k in seen_acts:
            add("duplicate_record", f"acts_and_sections entries {seen_acts[k]} and {i} are identical",
                path=f"acts_and_sections.{i}")
        seen_acts.setdefault(k, i)

    # ---- pages
    for p in data.pages:
        if p.mapping_status == "failed" or p.text_status == "failed":
            add("page_failed", p.note or "page could not be read or mapped; its fields may be missing",
                page=p.page, severity="warning")
        elif p.mapping_status == "partial" or p.text_status == "partial":
            add("page_partial", p.note or "page was only partly read or mapped", page=p.page)
        if p.unexpected_keys:
            add("unexpected_output_keys",
                f"the model returned keys outside the schema ({', '.join(p.unexpected_keys[:5])}); kept under other details",
                page=p.page)

    return StructuredValidation(is_valid=not any(i.severity == "error" for i in issues), issues=issues)


def _present(data: StructuredData, spec: str) -> bool:
    if "." in spec:
        v = data.get_value(spec)
        return bool(v and v.status != ValueStatus.NOT_FOUND)
    return bool(data.lists.get(spec))


def _check_page(page: Optional[int], known: set, path: str, add) -> None:
    if page is None:
        add("no_source_page", "value has no source page", severity="error", path=path)
    elif known and page not in known:
        add("bad_source_page", f"source page {page} is not a page of this document", severity="error", path=path,
            page=page)


def _check_format(kind: str, v: StructuredValue, path: str, add) -> None:
    text = v.value or ""
    if kind == "date" and compact(text) not in {"na", "nil"}:
        if parse_date(text) is None:
            code = "date_shape" if has_date_shape(text) else "date_format"
            add(code, "not a recognisable calendar date; shown exactly as printed", path=path, page=v.source_page)
            _flag(v, "date format not recognised")
    elif kind == "year" and not _YEAR.match(text.strip()):
        add("year_format", "not a four-digit year; shown exactly as printed", path=path, page=v.source_page)
        _flag(v, "year format not recognised")
    elif kind == "id" and not re.search(r"\d", text):
        add("identifier_format", "identifier has no digits; shown exactly as printed", path=path, page=v.source_page)
        _flag(v, "identifier format not recognised")
    elif kind == "section" and not _SECTION.search(text):
        add("section_format", "section has no number; shown exactly as printed", path=path, page=v.source_page)
        _flag(v, "section format not recognised")
    elif kind == "number" and not re.search(r"\d", text):
        add("number_format", "expected a number; shown exactly as printed", path=path, page=v.source_page)
        _flag(v, "number format not recognised")
    elif kind == "serial" and (len(text.strip()) > 12 or len(text.split()) > 3):
        add("serial_format", "a serial number is a short marker (1, A-2, LW-3); this looks like other text",
            path=path, page=v.source_page)
        _flag(v, "serial number looks like other text")
    if path.endswith(".name") and _RELATION.search(text):
        add("name_contains_relation", "the name field also holds a relationship marker (s/o, d/o, w/o), age or "
            "address; the parent / spouse and other details belong in their own fields", path=path, page=v.source_page)
        _flag(v, "name field contains a relationship marker or age")
    if path.endswith(".act") and re.search(r"\d", text):
        add("act_contains_digits", "an Act name should not contain section numbers; check act/section split",
            path=path, page=v.source_page)
        _flag(v, "act field contains digits")
