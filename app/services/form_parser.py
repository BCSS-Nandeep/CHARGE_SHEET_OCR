"""Stage: layout / form parsing for charge-sheet documents.

Works on a positioned document model (table rows, free text lines, header
label/value pairs) built from either native PDF text or OCR. Field meaning
comes from labels and local context; form numbers are only used to tell form
rows apart from numbered list items, never to decide what a field is.

The document is split into a *form zone* and a *narrative zone*. Primary
case identifiers (FIR number, dates, district, police station) are only
taken from the form zone, so crime numbers and dates that appear in the
investigation narrative cannot overwrite them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from app.schemas.charge_sheet import SourceConflict
from app.services.field_mapping_service import (
    HINT_PREFIX,
    best_canonical_match,
    is_generic_date_label,
    list_heading_kind,
)
from app.services.field_synonyms import NARRATIVE_MARKERS
from app.services.layout_service import HeaderPair, TableRow
from app.services.text_normalize import clean_value, compact, soften_pdf_text
from app.services.value_parsers import parse_date

_SERIAL = re.compile(r"^[\(\[]?0?(\d{1,2})[.)\]]?$")
_LEADING_SERIAL = re.compile(r"^\s*[\(\[]?0?(\d{1,2})\s*[.)\]]?\s+(?=\S)")
_NIL = re.compile(
    r"^(?:-+|—+|nil|n\.?\s*a\.?|none|not\s+applicable|mentioned\s+below\.?|as\s+follows|-do-|\.+|---)$",
    re.I,
)
_COLON_LABEL = re.compile(r"(?:(?<=^)|(?<=[\s,;]))([A-Za-z][A-Za-z.&/()' ]{0,45}?)\s*:\s*")
_COURT = re.compile(r"\bin\s*the\s*court\s*of\s*(.+)", re.I)
_FIR_REF = re.compile(
    r"\b(?:F\.?\s*I\.?\s*R\.?|Cr(?:ime)?\.?)\s*(?:No\.?|Number|NO\.?)\s*[:.\-]?\s*(\d{1,6}\s*/\s*(?:19|20)?\d{2})(?!\d)",
    re.I,
)
_REGISTRATION = re.compile(r"regist(?:er|ered|ration)", re.I)

# Fields that identify the case. Never taken from narrative prose.
PRIMARY_IDENTIFIERS = {
    "district",
    "police_station",
    "year",
    "fir_number",
    "crime_number",
    "fir_date",
    "charge_sheet_number",
    "submission_date",
}
# Person / list fields parsed by the entity stage, not as scalars.
PERSON_FIELDS = {"io_name", "complainant_name", "informant_name", "accused_name", "witness_name"}
# Origin preference when the same field has several candidates.
_ORIGIN_RANK = {
    "header_table": 5,
    "pdf_table": 5,
    "ocr_layout": 5,
    "text": 4,
    "inline": 4,
    "court_heading": 3,
    "narrative_kv": 2,
    "narrative_registration": 1,
    "derived": 0,
}


@dataclass
class RawCandidate:
    label: str
    value: str
    canonical: Optional[str]
    mapping_confidence: float
    source_page: int
    source_text: str
    origin: str = "text"
    zone: str = "form"
    form_hint: Optional[str] = None
    note: Optional[str] = None


@dataclass
class ListItem:
    kind: str  # accused_charged | accused_not_charged | witnesses
    serial: Optional[str]
    text: str
    role: Optional[str]
    page: int
    source_text: str


@dataclass
class DocItem:
    """One positioned element of a page in reading order."""

    page: int
    y: float
    text: str
    row: Optional[TableRow] = None
    header: Optional[HeaderPair] = None


@dataclass
class NarrativeText:
    """Narrative prose with page boundaries preserved for source attribution."""

    segments: List[Tuple[int, str]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(t for _, t in self.segments)

    def page_at(self, offset: int) -> Optional[int]:
        pos = 0
        for page, text in self.segments:
            end = pos + len(text) + 1
            if offset < end:
                return page
            pos = end
        return self.segments[-1][0] if self.segments else None

    @property
    def first_page(self) -> Optional[int]:
        return self.segments[0][0] if self.segments else None


@dataclass
class CaseReference:
    number: str
    page: Optional[int]
    context: str
    registration: bool


@dataclass
class FormParseResult:
    candidates: List[RawCandidate] = field(default_factory=list)
    unmapped: List[RawCandidate] = field(default_factory=list)
    lists: Dict[str, List[ListItem]] = field(default_factory=dict)
    acts_texts: List[Tuple[int, str]] = field(default_factory=list)
    narrative: NarrativeText = field(default_factory=NarrativeText)
    form_lines: List[Tuple[int, str]] = field(default_factory=list)
    narrative_start_page: Optional[int] = None
    case_references: List[CaseReference] = field(default_factory=list)
    # Narrative lines consumed as standalone 'Label: value' fields (kept out of brief facts)
    consumed_narrative: set = field(default_factory=set)


# ---------------------------------------------------------------------------
# Label / value helpers
# ---------------------------------------------------------------------------


def _is_nil(text: str) -> bool:
    return bool(_NIL.match(clean_value(text))) if text else True


def _recognise(label: str) -> Tuple[Optional[str], float]:
    """Canonical (or hint) for a label, or ('date', 1.0) for generic date labels."""
    if is_generic_date_label(label):
        return "date", 1.0
    return best_canonical_match(label, include_hints=True)


def _resolve_date(prev_canonical: Optional[str]) -> Optional[str]:
    """A bare 'Date'/'Dated'/'Dt.' takes its meaning from the field it follows."""
    if prev_canonical in {"fir_number", "crime_number", "year"}:
        return "fir_date"
    if prev_canonical in {"charge_sheet_number", "submission_date"}:
        return "submission_date"
    return None


def _label_like(label: str) -> bool:
    """Abbreviated labels ('Dist.', 'FIR. No.', 'P.S.', 'PS') — not prose words."""
    return label.endswith(".") or bool(re.fullmatch(r"[A-Z.]{2,6}", label))


def _prefix_label(part: str) -> Optional[Tuple[str, str, str, float]]:
    """Split 'FIR. No. 4/2017' / 'Dist. Northtown' / 'On 02.03.2017' into label + value.

    Without a colon, a leading phrase only counts as a label when it is
    abbreviation-like or is followed by a typed value (digits), so ordinary
    values ('Case property deposited in ...') are not split.
    """
    tokens = part.strip().split()
    if len(tokens) < 2:
        return None
    best = None
    for k in range(1, min(4, len(tokens) - 1) + 1):
        label = " ".join(tokens[:k])
        value = " ".join(tokens[k:])
        if not clean_value(value):
            continue
        if k == 1 and is_generic_date_label(label):
            if parse_date(value) or re.match(r"\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}", value):
                return label, value, "date", 1.0
            continue
        if not (_label_like(label) or value[:1].isdigit()):
            continue
        canon, score = best_canonical_match(label)
        if canon and score >= 0.9 and canon not in PERSON_FIELDS and (best is None or score > best[3]):
            best = (label, value, canon, score)
    return best


def _strip_serial(line: str) -> str:
    m = _LEADING_SERIAL.match(line + " ")
    return line[m.end():].strip() if m and m.end() <= len(line) else line


def parse_cell_pairs(text: str) -> Tuple[List[Tuple[str, str, str, float]], str]:
    """Find 'Label: value' and 'Label value' pairs inside one cell / line.

    Returns (pairs, leftover) where each pair is (label, value, canonical, score)
    and leftover is text that belongs to no recognised label.
    """
    pairs: List[Tuple[str, str, str, float]] = []
    leftovers: List[str] = []
    for raw_line in (text or "").split("\n"):
        line = _strip_serial(raw_line.strip())
        if not line:
            continue
        starts = []
        for m in _COLON_LABEL.finditer(line):
            label = m.group(1).strip()
            canon, score = _recognise(label)
            if canon and score >= 0.85:
                starts.append((m.start(1), m.end(), label, canon, score))
        segments: List[Tuple[Optional[Tuple[str, str, float]], str]] = []
        if not starts:
            segments.append((None, line))
        else:
            if starts[0][0] > 0:
                segments.append((None, line[: starts[0][0]]))
            for idx, (_, end_label, label, canon, score) in enumerate(starts):
                end = starts[idx + 1][0] if idx + 1 < len(starts) else len(line)
                segments.append(((label, canon, score), line[end_label:end]))
        for head, body in segments:
            current = head
            values: List[str] = []
            for part in re.split(r"[,;]\s+", body):
                pref = _prefix_label(part)
                if pref is None:
                    values.append(part)
                    continue
                if current is not None:
                    pairs.append((current[0], ", ".join(values), current[1], current[2]))
                elif values:
                    leftovers.append(", ".join(values))
                current = (pref[0], pref[2], pref[3])
                values = [pref[1]]
            if current is not None:
                pairs.append((current[0], ", ".join(values), current[1], current[2]))
            elif values and clean_value(", ".join(values)):
                leftovers.append(", ".join(values))
    return pairs, "\n".join(leftovers).strip()


def split_label_value(line: str) -> Tuple[Optional[str], Optional[str]]:
    """'Label : value', 'Label    value' (space aligned) or 'Label' alone."""
    raw = line.strip()
    if ":" in raw:
        a, b = raw.split(":", 1)
        if a.strip() and len(a) <= 60 and len(a.split()) <= 8:
            return a.strip(), b.strip()
    parts = re.split(r"\s{2,}", raw, maxsplit=1)
    if len(parts) == 2 and parts[1].strip() and len(parts[0]) <= 60:
        return parts[0].strip(), parts[1].strip()
    return None, None


# ---------------------------------------------------------------------------
# Zoning
# ---------------------------------------------------------------------------


def is_narrative_marker(text: str) -> bool:
    comp = compact(text)
    if not comp:
        return False
    return any(re.search(p, comp) for p in NARRATIVE_MARKERS)


def _is_prose(text: str) -> bool:
    words = text.split()
    return len(words) >= 10 and len(text) >= 70 and bool(re.search(r"[a-z]{3,}\s+[a-z]{2,}\s+[a-z]{2,}", text))


def find_narrative_start(items: Sequence[DocItem]) -> Optional[int]:
    """Index of the first narrative item, or None if the document has none."""
    for i, item in enumerate(items):
        if item.row is not None:
            cells = _row_body(item.row)[1]
            if cells and best_canonical_match(cells[0])[0] == "brief_facts":
                return i
            continue
        if is_narrative_marker(item.text):
            return i
        # Prose block fallback: three consecutive long sentence lines
        if i >= 3 and _is_prose(item.text):
            run = [it for it in items[i : i + 3] if it.row is None and _is_prose(it.text)]
            if len(run) == 3:
                return i
    return None


# ---------------------------------------------------------------------------
# Row interpretation
# ---------------------------------------------------------------------------


def _row_body(row: TableRow) -> Tuple[Optional[int], List[str]]:
    cells = [soften_pdf_text(c) for c in row.cells if c and c.strip()]
    if cells and _SERIAL.match(cells[0].strip()):
        return int(_SERIAL.match(cells[0].strip()).group(1)), cells[1:]
    return None, cells


def _split_heading_cell(cell: str) -> Tuple[str, str]:
    """'Particulars of the accused:\\nA1. X ...' → (heading, items text)."""
    lines = cell.split("\n")
    for i, line in enumerate(lines):
        if line.rstrip().endswith(":"):
            return "\n".join(lines[: i + 1]).rstrip(":").strip(), "\n".join(lines[i + 1 :]).strip()
    # No colon: heading is the leading lines that still read as a heading
    for i in range(len(lines), 0, -1):
        head = " ".join(lines[:i])
        if list_heading_kind(head) and len(head.split()) <= 12 and not re.search(r"(?i)\b(?:s/o|age|r/o)\b", head):
            return head.strip(), "\n".join(lines[i:]).strip()
    return cell.strip(), ""


class _FormState:
    def __init__(self) -> None:
        self.last_form_serial: Optional[int] = None
        self.list_kind: Optional[str] = None
        self.list_from_rows = False  # a table list never continues into free text
        self.suspended_list: Optional[str] = None  # ...but may resume in the next table
        self.last_item_serial: Optional[int] = None
        self.prev_canonical: Optional[str] = None


def _row_kind(serial: Optional[int], cells: List[str], state: _FormState) -> str:
    """'form' or 'item' for one table row, from sequence + label semantics."""
    label = cells[0] if cells else ""
    canon, score = _recognise(label.split("\n")[0] if "\n" in label and len(label) > 80 else label)
    heading = list_heading_kind(label) if len(label) < 120 else None
    looks_label = (canon is not None and score >= 0.85) or heading is not None
    if state.list_kind is None:
        resumes = (
            state.suspended_list is not None
            and serial is not None
            and state.last_item_serial is not None
            and serial == state.last_item_serial + 1
            and not looks_label
        )
        if resumes:
            state.list_kind, state.list_from_rows = state.suspended_list, True
            return "item"
        return "form"
    if serial is None:
        return "item"
    expected_item = 1 if state.last_item_serial is None else state.last_item_serial + 1
    next_form = state.last_form_serial + 1 if state.last_form_serial is not None else None
    if looks_label and not re.search(r"(?i)\b(?:s/o|d/o|w/o|age|r/o)\b", label):
        return "form"
    if serial == expected_item:
        return "item"
    if next_form is not None and serial == next_form:
        return "form"
    return "item"


def _emit_pairs(
    pairs: List[Tuple[str, str, str, float]],
    *,
    page: int,
    source: str,
    origin: str,
    form_hint: Optional[str],
    state: _FormState,
    out: FormParseResult,
) -> None:
    for label, value, canon, score in pairs:
        if canon == "date":
            resolved = _resolve_date(state.prev_canonical)
            if resolved is None:
                out.unmapped.append(
                    RawCandidate(label, clean_value(value), None, 0.5, page, source[:300], origin, form_hint=form_hint)
                )
                continue
            canon, score = resolved, 0.8
            note = f"generic date label resolved from preceding field ({state.prev_canonical})"
        else:
            note = None
        _add_candidate(label, value, canon, score, page=page, source=source, origin=origin,
                       form_hint=form_hint, out=out, note=note)
        if not canon.startswith(HINT_PREFIX):
            state.prev_canonical = canon


def _add_candidate(
    label: str,
    value: str,
    canon: Optional[str],
    score: float,
    *,
    page: int,
    source: str,
    origin: str,
    form_hint: Optional[str],
    out: FormParseResult,
    zone: str = "form",
    note: Optional[str] = None,
) -> None:
    value_c = value.strip()
    if not value_c or _is_nil(value_c):
        if canon and canon.startswith(HINT_PREFIX) and value_c:
            out.unmapped.append(RawCandidate(label, clean_value(value_c), None, score, page, source[:300], origin, zone, form_hint))
        return
    if canon is None or canon.startswith(HINT_PREFIX):
        out.unmapped.append(RawCandidate(label, clean_value(value_c), None, score, page, source[:300], origin, zone, form_hint))
        return
    if canon == "acts_and_sections":
        out.acts_texts.append((page, f"{label}: {value_c}"))
    if canon in {"fir_number", "crime_number"}:
        # Compound field ("FIR No. & Date: 4/2017 dt. 02-03-2017") → number + FIR date
        num = re.search(r"(?<![\d.\-])\d{1,6}\s*/\s*\d{2,4}(?![\d.\-/])", value_c)
        date_m = re.search(r"\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}", value_c)
        if num and date_m and parse_date(date_m.group(0)) and (num.end() <= date_m.start() or num.start() >= date_m.end()):
            out.candidates.append(
                RawCandidate(label, date_m.group(0), "fir_date", min(score, 0.85), page, source[:300], origin, zone,
                             form_hint, "date given in the same field as the FIR number")
            )
            value_c = num.group(0)
    out.candidates.append(
        RawCandidate(label, value_c, canon, score, page, source[:300], origin, zone, form_hint, note)
    )


def _interpret_form_row(serial: Optional[int], cells: List[str], row: TableRow, state: _FormState, out: FormParseResult) -> None:
    page = row.page
    source = " | ".join(c.replace("\n", " ") for c in cells)
    form_hint = f"col_{serial:02d}" if serial is not None else None
    label_cell = cells[0] if cells else ""
    value_text = "\n".join(cells[1:]).strip()

    heading_kind = list_heading_kind(label_cell.split(":")[0]) if label_cell else None
    canon, score = _recognise(label_cell) if label_cell else (None, 0.0)
    if heading_kind and not (canon and score >= 0.95 and canon not in {"accused_name", "witness_name"}):
        head, items_text = _split_heading_cell(label_cell)
        state.list_kind = heading_kind
        state.list_from_rows = True
        state.last_item_serial = None
        body = "\n".join(t for t in (items_text, value_text) if t).strip()
        if body and not _is_nil(body):
            out.lists.setdefault(heading_kind, []).append(
                ListItem(heading_kind, None, body, None, page, f"{head}: {body}"[:400])
            )
        return

    state.list_kind = None
    if canon == "acts_and_sections":
        out.acts_texts.append((page, " ".join(cells)))
        return

    # Label cell may itself hold 'District: X / Police Station: Y'
    label_pairs, label_rest = parse_cell_pairs(label_cell)
    if label_pairs:
        _emit_pairs(label_pairs, page=page, source=source, origin=row.origin, form_hint=form_hint, state=state, out=out)
        canon, score = _recognise(label_rest) if label_rest else (None, 0.0)
        label_cell = label_rest

    if canon in PERSON_FIELDS:
        _add_candidate(label_cell, value_text, canon, score, page=page, source=source, origin=row.origin,
                       form_hint=form_hint, out=out)
        state.prev_canonical = canon
        return

    # Value cells may embed further labelled fields ('03. Dated: 05.06.2024')
    value_pairs, value_rest = parse_cell_pairs(value_text)
    if canon == "date":
        canon = _resolve_date(state.prev_canonical)
        score = 0.8
    if label_cell:
        if canon is not None:
            _add_candidate(label_cell, value_rest if value_pairs else value_text, canon, score,
                           page=page, source=source, origin=row.origin, form_hint=form_hint, out=out)
            if not canon.startswith(HINT_PREFIX):
                state.prev_canonical = canon
        elif clean_value(value_rest if value_pairs else value_text):
            out.unmapped.append(
                RawCandidate(label_cell, clean_value(value_rest if value_pairs else value_text), None, score,
                             page, source[:300], row.origin, form_hint=form_hint)
            )
    _emit_pairs(value_pairs, page=page, source=source, origin=row.origin, form_hint=form_hint, state=state, out=out)


def _interpret_rows(rows: List[TableRow], state: _FormState, out: FormParseResult) -> None:
    for row in rows:
        serial, cells = _row_body(row)
        if not cells:
            continue
        kind = _row_kind(serial, cells, state)
        if kind == "item":
            role = "\n".join(cells[1:]).strip() or None
            item = ListItem(
                state.list_kind or "witnesses",
                str(serial) if serial is not None else None,
                cells[0],
                clean_value(role) if role else None,
                row.page,
                " | ".join(c.replace("\n", " ") for c in cells)[:400],
            )
            items = out.lists.setdefault(item.kind, [])
            if serial is None and items:
                items[-1].text += "\n" + cells[0]
            else:
                items.append(item)
            if serial is not None:
                state.last_item_serial = serial
            continue
        if serial is not None:
            state.last_form_serial = serial
        state.suspended_list = None
        _interpret_form_row(serial, cells, row, state, out)


# ---------------------------------------------------------------------------
# Free text lines (layouts without detected tables)
# ---------------------------------------------------------------------------


def _interpret_text_lines(lines: List[Tuple[int, str]], state: _FormState, out: FormParseResult, *, zone: str) -> None:
    pending: Optional[Tuple[str, str, float, int, List[str]]] = None  # label, canon, score, page, values

    def flush_pending() -> None:
        nonlocal pending
        if pending is None:
            return
        label, canon, score, page, values = pending
        pending = None
        if values:
            _add_candidate(label, " ".join(values), canon, score, page=page, source=f"{label} {' '.join(values)}",
                           origin="text", form_hint=None, out=out, zone=zone)

    for page, raw in lines:
        text = soften_pdf_text(raw).strip()
        if not text:
            continue
        m = _LEADING_SERIAL.match(text + " ")
        serial = int(m.group(1)) if m else None
        body = text[m.end():].strip() if m else text

        label, value = split_label_value(body)
        head = label if label is not None else body
        canon, score = _recognise(head) if len(head) <= 80 else (None, 0.0)

        if zone == "narrative":
            # Only standalone 'Label: value' lines; never identifiers or lists from prose
            if (
                label and canon and score >= 0.9 and value and len(text) <= 100
                and canon not in PRIMARY_IDENTIFIERS and canon not in PERSON_FIELDS
                and not canon.startswith(HINT_PREFIX) and canon != "date"
                and canon not in {"brief_facts", "investigation_narrative", "final_conclusions"}
            ):
                _add_candidate(label, value, canon, score, page=page, source=text, origin="narrative_kv",
                               form_hint=None, out=out, zone=zone)
                out.consumed_narrative.add((page, raw))
            continue

        heading = list_heading_kind(head) if len(head) <= 80 and not re.search(r"(?i)\b(?:s/o|d/o|w/o|age|r/o)\b", head) else None
        if heading and (canon is None or canon in {"accused_name", "witness_name"} or score < 0.95):
            flush_pending()
            state.list_kind = heading
            state.last_item_serial = None
            if value and not _is_nil(value):
                out.lists.setdefault(heading, []).append(ListItem(heading, None, value, None, page, text[:400]))
            continue

        if label is not None and canon is not None and score >= 0.8:
            flush_pending()
            state.list_kind = None
            if canon == "date":
                canon, score = _resolve_date(state.prev_canonical), 0.8
            if canon == "acts_and_sections":
                out.acts_texts.append((page, text))
            if canon is None:
                out.unmapped.append(RawCandidate(label, clean_value(value or ""), None, 0.5, page, text[:300], "text"))
                continue
            if value:
                if canon in PERSON_FIELDS:
                    _add_candidate(label, value, canon, score, page=page, source=text, origin="text",
                                   form_hint=None, out=out, zone=zone)
                else:
                    pairs, rest = parse_cell_pairs(value)
                    _add_candidate(label, rest if pairs else value, canon, score, page=page, source=text,
                                   origin="text", form_hint=None, out=out, zone=zone)
                    if not canon.startswith(HINT_PREFIX):
                        state.prev_canonical = canon
                    _emit_pairs(pairs, page=page, source=text, origin="inline", form_hint=None, state=state, out=out)
                    continue
                if not canon.startswith(HINT_PREFIX):
                    state.prev_canonical = canon
            else:
                pending = (label, canon, score, page, [])
            continue

        # Label-only line ('Name and Rank of I.O.') — value follows on next lines.
        # Needs a near-exact label match: a title like 'Charge sheet' is not a field.
        if label is None and canon is not None and score >= 0.95 and len(body.split()) <= 12 and not state.list_kind:
            flush_pending()
            pending = (body.rstrip(":.,"), canon, score, page, [])
            continue

        # Inline pairs without colon ('Dist. Northtown', 'FIR. No. 4/2017')
        if state.list_kind is None and pending is None:
            pairs, rest = parse_cell_pairs(body)
            if pairs:
                _emit_pairs(pairs, page=page, source=text, origin="inline", form_hint=None, state=state, out=out)
                continue

        if state.list_kind:
            items = out.lists.setdefault(state.list_kind, [])
            items.append(ListItem(state.list_kind, str(serial) if serial else None, body, None, page, text[:400]))
            continue
        if pending is not None and len(pending[4]) < 3:
            pending[4].append(body)
            continue
        flush_pending()
    flush_pending()


def merge_list_lines(items: List[ListItem]) -> List[ListItem]:
    """Join continuation lines (no serial) of text-mode lists onto their item."""
    merged: List[ListItem] = []
    for item in items:
        if merged and item.serial is None and merged[-1].serial is not None and item.role is None:
            merged[-1].text += "\n" + item.text
            merged[-1].source_text = (merged[-1].source_text + " " + item.text)[:400]
        else:
            merged.append(item)
    return merged


# ---------------------------------------------------------------------------
# Document-level parse
# ---------------------------------------------------------------------------


def _court_candidates(items: Sequence[DocItem], out: FormParseResult) -> None:
    for idx, item in enumerate(items):
        if item.row is not None:
            continue
        text = soften_pdf_text(item.text)
        m = _COURT.search(text) or _COURT.search(re.sub(r"(?i)inthecourtof", "in the court of ", text))
        if not m:
            continue
        name = m.group(1).strip()
        # A heading that wraps onto the next line ("... OF FIRST" / "CLASS, AT X.")
        if not re.search(r"[.]\s*$", name) and idx + 1 < len(items) and items[idx + 1].row is None:
            nxt = soften_pdf_text(items[idx + 1].text).strip()
            if nxt and len(nxt) <= 60 and nxt.upper() == nxt:
                name = f"{name} {nxt}"
        name = re.sub(r"(?i)^hon'?\s*ble\s*", "", clean_value(name))
        if 4 <= len(name) <= 140:
            out.candidates.append(
                RawCandidate("In the court of", name, "court_name", 0.85, item.page, text[:300], "court_heading")
            )
        return


def _case_references(narrative: NarrativeText) -> List[CaseReference]:
    refs: List[CaseReference] = []
    text = narrative.text
    for m in _FIR_REF.finditer(text):
        number = re.sub(r"\s+", "", m.group(1))
        start = max(0, m.start() - 120)
        context = re.sub(r"\s+", " ", text[start : m.end() + 60]).strip()
        before = text[max(0, m.start() - 90) : m.start()]
        refs.append(
            CaseReference(
                number=number,
                page=narrative.page_at(m.start()),
                context=context,
                registration=bool(_REGISTRATION.search(before)),
            )
        )
    return refs


def parse_document(pages: Sequence[Sequence[DocItem]]) -> FormParseResult:
    out = FormParseResult()
    items: List[DocItem] = [it for page_items in pages for it in page_items]
    start = find_narrative_start(items)
    if start is not None:
        out.narrative_start_page = items[start].page
    form_items = items if start is None else items[:start]
    narrative_items = [] if start is None else items[start:]

    state = _FormState()
    pending_lines: List[Tuple[int, str]] = []
    header_pairs: List[HeaderPair] = []

    def flush_lines() -> None:
        if pending_lines:
            if state.list_from_rows:
                state.suspended_list = state.list_kind
                state.list_kind = None
                state.list_from_rows = False
            _interpret_text_lines(list(pending_lines), state, out, zone="form")
            pending_lines.clear()

    for item in form_items:
        out.form_lines.append((item.page, item.text))
        if item.header is not None:
            flush_lines()
            header_pairs.append(item.header)
            continue
        if header_pairs:
            _emit_header_pairs(header_pairs, state, out)
            header_pairs = []
        if item.row is not None:
            flush_lines()
            _interpret_rows([item.row], state, out)
        else:
            pending_lines.append((item.page, item.text))
    if header_pairs:
        _emit_header_pairs(header_pairs, state, out)
    flush_lines()

    # Narrative: prose for brief facts + standalone labelled lines only
    for item in narrative_items:
        if item.row is not None:
            serial, cells = _row_body(item.row)
            if cells:
                _interpret_rows([item.row], state, out)
            continue
        out.narrative.segments.append((item.page, soften_pdf_text(item.text)))
    narrative_state = _FormState()
    _interpret_text_lines(
        [(p, t) for p, t in out.narrative.segments], narrative_state, out, zone="narrative"
    )

    _court_candidates(items, out)
    out.case_references = _case_references(out.narrative)
    for kind in list(out.lists):
        out.lists[kind] = merge_list_lines(out.lists[kind])
    return out


def _emit_header_pairs(pairs: List[HeaderPair], state: _FormState, out: FormParseResult) -> None:
    ordered = sorted(pairs, key=lambda p: (p.page, p.order))
    tuples = []
    for p in ordered:
        canon, score = _recognise(p.label)
        if canon:
            tuples.append((p.label, p.value, canon, score))
    if tuples:
        source = " | ".join(f"{p.label}: {p.value}" for p in ordered)
        _emit_pairs(tuples, page=ordered[0].page, source=source, origin="header_table",
                    form_hint=None, state=state, out=out)


# ---------------------------------------------------------------------------
# Candidate selection with conflict preservation
# ---------------------------------------------------------------------------


def plausible(canonical: str, value: str) -> bool:
    """Type/format check used to rank candidates (full validation happens later)."""
    v = clean_value(value)
    if not v:
        return False
    if canonical in {"district", "police_station"}:
        return (
            bool(re.search(r"[A-Za-z]{3,}", v))
            and len(v.split()) <= 6
            and not re.search(r"\d{3,}", v)
            and not re.search(r"(?i)\b(?:s/o|age|caste|occ|r/o|accused|witness|committed)\b", v)
        )
    if canonical == "year":
        return bool(re.fullmatch(r"(?:19|20)\d{2}", v))
    if canonical in {"fir_number", "crime_number"}:
        return bool(re.search(r"\d", v)) and len(v) <= 30 and not parse_date(v)
    if canonical == "charge_sheet_number":
        return bool(re.search(r"\d", v)) and len(v) <= 30
    if canonical in {"fir_date", "submission_date"}:
        return parse_date(v) is not None
    if canonical == "court_name":
        return len(v) <= 160
    if canonical in {"final_report_type", "supplementary_or_original"}:
        return len(v) <= 60
    return True


def value_key(canonical: str, value: str) -> str:
    if canonical in {"fir_date", "submission_date"}:
        d = parse_date(value)
        if d:
            return d.isoformat()
    if canonical in {"fir_number", "crime_number", "charge_sheet_number"}:
        return re.sub(r"[^0-9/]", "", value)
    return compact(value)


@dataclass
class SelectedField:
    candidate: RawCandidate
    alternates: List[RawCandidate]
    conflict: Optional[SourceConflict] = None


def select_fields(candidates: List[RawCandidate]) -> Dict[str, SelectedField]:
    by_canon: Dict[str, List[RawCandidate]] = {}
    for c in candidates:
        if c.canonical:
            by_canon.setdefault(c.canonical, []).append(c)
    selected: Dict[str, SelectedField] = {}
    for canon, cands in by_canon.items():
        if canon in PRIMARY_IDENTIFIERS:
            cands = [c for c in cands if c.zone == "form" or c.origin in {"narrative_registration", "derived"}]
        if not cands:
            continue
        groups: Dict[str, List[RawCandidate]] = {}
        for c in cands:
            key = value_key(canon, c.value) or compact(c.value)
            groups.setdefault(key, []).append(c)

        def rank(group: List[RawCandidate]) -> Tuple:
            best = max(group, key=lambda c: (_ORIGIN_RANK.get(c.origin, 1), c.mapping_confidence))
            return (
                plausible(canon, best.value),
                _ORIGIN_RANK.get(best.origin, 1),
                len(group),
                best.mapping_confidence,
            )

        ordered = sorted(groups.values(), key=rank, reverse=True)
        top = ordered[0]
        chosen = max(top, key=lambda c: (_ORIGIN_RANK.get(c.origin, 1), c.mapping_confidence))
        alternates = [max(g, key=lambda c: c.mapping_confidence) for g in ordered[1:]]
        conflict = None
        rivals = [
            a for a in alternates
            if plausible(canon, a.value)
            and _ORIGIN_RANK.get(a.origin, 1) >= 3
            and not _same_value(canon, a.value, chosen.value)
        ]
        if rivals and plausible(canon, chosen.value):
            conflict = SourceConflict(
                field_name=canon,
                values=[clean_value(chosen.value)] + [clean_value(r.value) for r in rivals],
                source_pages=sorted({chosen.source_page} | {r.source_page for r in rivals}),
                evidence="Different values for the same field in the form section of the source",
                resolution="unresolved",
            )
        selected[canon] = SelectedField(chosen, alternates, conflict)
    return selected


def _same_value(canon: str, a: str, b: str) -> bool:
    ka, kb = value_key(canon, a), value_key(canon, b)
    if not ka or not kb:
        return False
    return ka == kb or (len(min(ka, kb, key=len)) >= 4 and (ka in kb or kb in ka))
