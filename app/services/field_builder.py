"""Build the field list of one PDF: labels and values as printed, in reading order.

There is no fixed list of fields. The document is read item by item (header
table pairs, form rows, free lines, page after page) and:

* a label starts a new field — the label cell of a form row, a header-table
  label, the text before a colon, or a heading line with its value below;
* everything after a label belongs to it (text beside it, lines below it,
  lines on the following pages) until the next label starts;
* rows under a list heading ("Particulars of the witnesses") are list items
  and stay in that heading's value;
* from the start of the narrative ("Brief facts of the case", "Honoured Sir,")
  the running text is one value — sentences such as "Age: 30" inside it are
  not split into fields;
* text before the first label is kept as a field without a label.

Label and value text is never renamed or rewritten; only wrapped label lines
are joined and missing spaces of glued PDF exports are restored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from app.schemas.charge_sheet import PdfField
from app.services.field_mapping_service import list_heading_kind
from app.services.form_parser import (
    DocItem,
    colon_label,
    find_narrative_start,
    is_heading,
    parse_cell_pairs,
    recognise,
    row_body,
    strip_serial,
)
from app.services.layout_service import is_court_heading
from app.services.text_normalize import join_narrative_lines, soften_pdf_text

# Person details make a cell a value even when it starts like a label
_PERSON_MARKERS = re.compile(r"(?i)\b(?:s/o|d/o|w/o|r/o|age\s*[:\d]|sri\.?|smt\.?)\b|\bA\s*-?\s*\d+\s*[).]")
MAX_VALUE_CHARS = 30000


@dataclass
class _Open:
    label: Optional[str]
    page: int
    source: str
    serial: Optional[str] = None
    parts: List[str] = field(default_factory=list)
    pages: List[int] = field(default_factory=list)
    is_list: bool = False
    list_from_rows: bool = False  # a list built from table rows never continues into free text
    narrative: bool = False


def _one_line(text: str) -> str:
    """A label wrapped over several lines in its cell → one line."""
    return re.sub(r"\s*\n\s*", " ", text).strip()


def _content_cells(cells: List[str]) -> List[str]:
    """Row cells as one-line texts, without separator columns (':', '•') that hold no letters or digits."""
    return [_one_line(c) for c in cells if re.search(r"[A-Za-z0-9]", c)]


def _label_text(text: str) -> str:
    return _one_line(text).rstrip(":").strip()


def _row_label_like(label_cell: str) -> bool:
    first = label_cell.split("\n", 1)[0]
    if _PERSON_MARKERS.search(label_cell[:80]):
        return False
    for t in {label_cell, first}:
        t = _label_text(t)
        canon, score = recognise(t)
        if (canon and score >= 0.85) or list_heading_kind(t) or is_heading(t):
            return True
    return False


def _is_title_line(line: str) -> bool:
    """A short line printed in capitals ('AT NORTHTOWN.', 'JUDICIAL FIRST CLASS MAGISTRATE')."""
    letters = [c for c in line if c.isalpha()]
    return 2 <= len(letters) and len(line.split()) <= 14 and sum(c.isupper() for c in letters) >= 0.8 * len(letters)


def _space_aligned(line: str) -> Optional[tuple]:
    """'Name of Court    Sessions Court' — label and value separated by a column gap."""
    parts = re.split(r"\s{2,}", line.strip(), maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return None
    label = parts[0].strip()
    canon, score = recognise(label)
    if (canon and score >= 0.85) or is_heading(label):
        return label, parts[1].strip()
    return None


class FieldBuilder:
    def __init__(self, page_confidence: Optional[Dict[int, Optional[float]]] = None) -> None:
        self.page_confidence = page_confidence or {}
        self.fields: List[PdfField] = []
        self._cur: Optional[_Open] = None
        self._per_page: Dict[int, int] = {}

    # -- field lifecycle -------------------------------------------------

    def _close(self) -> None:
        cur, self._cur = self._cur, None
        if cur is None:
            return
        if cur.narrative:
            value = join_narrative_lines([ln for part in cur.parts for ln in part.split("\n")])
        else:
            value = "\n".join(p for p in cur.parts if p.strip()).strip()
        if cur.label is None and not value:
            return
        n = self._per_page.get(cur.page, 0) + 1
        self._per_page[cur.page] = n
        pages = sorted(set([cur.page] + cur.pages))
        confs = [c for c in (self.page_confidence.get(p) for p in pages) if c is not None]
        self.fields.append(
            PdfField(
                id=f"p{cur.page}-{n:02d}",
                page=cur.page,
                pages=pages,
                serial=cur.serial,
                label=cur.label,
                value=value[:MAX_VALUE_CHARS],
                source=cur.source,
                text_block=cur.narrative,
                confidence=round(min(confs), 3) if confs else None,
                notes=(f"value truncated to {MAX_VALUE_CHARS} characters" if len(value) > MAX_VALUE_CHARS else None),
            )
        )

    def _open(self, label: Optional[str], value: str, page: int, source: str, serial: Optional[str] = None) -> _Open:
        self._close()
        label = _label_text(label) if label else None
        self._cur = _Open(label=label or None, page=page, source=source, serial=serial,
                          is_list=bool(label and list_heading_kind(label)))
        if value and value.strip():
            self._cur.parts.append(value.strip())
        return self._cur

    def _append(self, text: str, page: int) -> None:
        if not text.strip():
            return
        if self._cur is None:
            self._open(None, "", page, "text")
        self._cur.parts.append(text.strip())
        if page not in self._cur.pages:
            self._cur.pages.append(page)

    # -- items ------------------------------------------------------------

    def build(self, pages: Sequence[Sequence[DocItem]]) -> List[PdfField]:
        items = [it for page in pages for it in page]
        start = find_narrative_start(items)
        # IR Annexure-I descriptive prose before Part-II tables: keep as one
        # text block (do not split "Age: 16" / "Caste: Goud" inside the story).
        preamble_end = _ir_preamble_end(items)
        for idx, it in enumerate(items):
            if start is not None and idx >= start:
                self._narrative_item(it, first=(idx == start))
            elif preamble_end is not None and idx < preamble_end:
                if it.header is not None or it.row is not None:
                    # Form content should never sit inside the preamble window
                    if it.header is not None:
                        self._open(it.header.label, it.header.value, it.page, "header_table")
                    else:
                        self._form_row(it)
                else:
                    self._append(soften_pdf_text(it.text), it.page)
                    if self._cur is not None:
                        self._cur.narrative = True
            elif it.header is not None:
                self._open(it.header.label, it.header.value, it.page, "header_table")
            elif it.row is not None:
                self._form_row(it)
            else:
                self._form_line(it)
        self._close()
        return self.fields

    def _form_row(self, it: DocItem) -> None:
        serial, cells = row_body(it.row)
        if not cells:
            return
        label_cell, value_cells = cells[0], cells[1:]
        cur = self._cur
        if cur is not None and cur.is_list and not _row_label_like(label_cell):
            # A numbered entry under a list heading (one witness / accused per row)
            self._append(" | ".join(([serial] if serial else []) + _content_cells(cells)), it.page)
            cur.list_from_rows = True
            return
        # Whole-cell label ("PAN Card", "Present Address") must not be split by
        # abbreviation prefix rules ("PAN" + "Card").
        whole_canon, whole_score = recognise(_label_text(label_cell.split("\n", 1)[0]))
        if whole_canon and whole_score >= 0.9:
            pairs, rest = [], ""
        else:
            pairs, rest = parse_cell_pairs(label_cell)
        if pairs and not rest and all(value for _, value in pairs):
            # The label cell holds its own 'Dist. X' / 'District: X. Police Station: Y' pairs
            for i, (label, value) in enumerate(pairs):
                self._open(label, value, it.page, "form_row", serial if i == 0 else None)
            self._value_cells(value_cells, it.page, main=None)
            return
        if not _row_label_like(label_cell):
            if cur is not None and serial is None:
                # A row without a number or label continues the previous value
                self._append(" | ".join(_content_cells(cells)), it.page)
                return
        label, lead = label_cell, ""
        lines = label_cell.split("\n")
        if len(lines) > 1 and (lines[0].rstrip().endswith(":") or _PERSON_MARKERS.search("\n".join(lines[1:]))):
            # 'Particulars of the accused:\nA1. X ...' — heading line, entries below it
            label, lead = lines[0], "\n".join(lines[1:])
        main = self._open(label, lead, it.page, "form_row", serial)
        self._value_cells(value_cells, it.page, main=main)

    def _value_cells(self, cells: List[str], page: int, main: Optional[_Open]) -> None:
        for cell in cells:
            pairs, rest = parse_cell_pairs(cell)
            if pairs and not rest:
                # The cell starts with its own label ('03. Dated: 05.06.2024')
                for label, value in pairs:
                    self._open(label, value, page, "form_row")
                main = None
                continue
            if main is not None and self._cur is main:
                main.parts.append(cell)
            else:
                self._append(cell, page)

    def _form_line(self, it: DocItem) -> None:
        text = soften_pdf_text(it.text).strip()
        if len(re.findall(r"[A-Za-z0-9]", text)) < 2:
            return  # OCR speck ("N", "'0", a stray comma)
        serial, body = strip_serial(text)
        if serial is not None and not (is_heading(body) or colon_label(body)):
            serial, body = None, text  # a number that starts a sentence, not a form number
        cur = self._cur
        if cur is not None and cur.is_list:
            if _row_label_like(body):
                pair = colon_label(body)
                self._open(pair[0] if pair else body, pair[1] if pair else "", it.page,
                           "label_colon" if pair else "heading", serial)
            elif cur.list_from_rows:
                self._open(None, text, it.page, "text")  # free text after a table list is not a list entry
            else:
                self._append(text, it.page)  # list entries keep their own "Age: 30" details
            return
        pairs, rest = parse_cell_pairs(body)
        if pairs:
            if rest:
                self._append(rest, it.page)
            for i, (label, value) in enumerate(pairs):
                self._open(label, value, it.page, "label_colon", serial if i == 0 else None)
            return
        pair = colon_label(body) or _space_aligned(body)
        if pair:
            self._open(pair[0], pair[1], it.page, "label_colon", serial)
            return
        if is_heading(body):
            self._open(body, "", it.page, "heading", serial)
            return
        self._append(text, it.page)

    def _narrative_item(self, it: DocItem, first: bool) -> None:
        if it.row is not None:
            serial, cells = row_body(it.row)
            if not cells:
                return
            if first:
                lines = cells[0].split("\n")
                cur = self._open(lines[0], "\n".join(lines[1:]), it.page, "form_row", serial)
                cur.narrative = True
                for cell in cells[1:]:
                    cur.parts.append(cell)
                return
            canon, score = recognise(_label_text(cells[0]))
            # Resume the form when a labelled row appears. IR templates often
            # leave the serial column blank, so do not require a printed number.
            if (
                canon
                and score >= 0.9
                and canon != "brief_facts"
                and (len(cells) > 1 or serial is not None or _row_label_like(cells[0]))
            ):
                self._form_row(it)
                return
            if is_heading(cells[0]) and len(cells) >= 1:
                self._form_row(it)
                return
            self._narrative_text(" ".join(([serial] if serial else []) + cells), it.page)
            return
        text = soften_pdf_text(it.text).strip()
        if first:
            serial, body = strip_serial(text)
            label_part, colon, after = body.partition(":")
            if colon and is_heading(label_part + ":"):
                # '10. Brief facts of the case: On <date> the complainant ...'
                self._open(label_part, after, it.page, "heading", serial).narrative = True
                return
            if is_heading(body):
                self._open(body, "", it.page, "heading", serial).narrative = True
                return
            cur = self._cur
            heading = self._take_heading_lines()
            if heading:
                # 'IN THE HON'BLE ... COURT AT X.' printed above 'Honoured Sir,' names the narrative
                cur = self._open(heading, "", it.page, "heading")
            elif cur is None or cur.parts or cur.label is None:
                cur = self._open(None, "", it.page, "text")
            cur.narrative = True
        self._narrative_text(text, it.page)

    def _take_heading_lines(self) -> Optional[str]:
        """Remove and return the heading lines that end the current value, if any.

        A court heading ('IN THE COURT OF ...') — possibly wrapped onto further
        all-capital lines ('AT NORTHTOWN.') — directly above the start of an unlabelled
        narrative is that narrative's heading, not part of the previous value.
        """
        cur = self._cur
        if cur is not None and not cur.parts and cur.label and is_court_heading(cur.label):
            self._cur = None
            return cur.label
        if cur is None or not cur.parts:
            return None
        lines = [ln for part in cur.parts for ln in part.split("\n")]
        start = len(lines)
        while start > 0 and (_is_title_line(lines[start - 1]) or is_court_heading(lines[start - 1])):
            start -= 1
        taken = lines[start:]
        if start == 0 and cur.label and is_court_heading(cur.label):
            # The heading's first line was read as a label and its wrapped lines as the value
            self._cur = None
            return " ".join([cur.label] + [ln.strip() for ln in taken])
        if not taken or not any(is_court_heading(ln) for ln in taken):
            return None
        cur.parts = ["\n".join(lines[:start])] if start else []
        if cur.label is None and not cur.parts:
            self._cur = None  # the field held only the heading
        return " ".join(ln.strip() for ln in taken)

    def _narrative_text(self, text: str, page: int) -> None:
        for line in text.split("\n"):
            # OCR specks from stamps / signatures ("a", ",", "@ze", CJK glyphs)
            if len(re.findall(r"[A-Za-z0-9]", line)) >= 3:
                self._append(line, page)
        if self._cur is not None:
            self._cur.narrative = True


_PART_II = re.compile(r"(?i)(?:annexure\s*[-–]?\s*ii|individual\s*particulars|unique\s*identifiers)")


def _ir_preamble_end(items: Sequence[DocItem]) -> Optional[int]:
    """Index where IR Part-II / form content begins, else None."""
    first_form = next((i for i, it in enumerate(items) if it.row is not None or it.header is not None), None)
    part_ii = next(
        (
            i
            for i, it in enumerate(items)
            if it.row is None and it.header is None and _PART_II.search(it.text or "")
        ),
        None,
    )
    if first_form is None and part_ii is None:
        return None
    if first_form is not None and part_ii is not None:
        return min(first_form, part_ii)
    return first_form if first_form is not None else part_ii


def build_fields(pages: Sequence[Sequence[DocItem]], page_confidence: Optional[Dict[int, Optional[float]]] = None) -> List[PdfField]:
    return FieldBuilder(page_confidence).build(pages)
