"""Layout reconstruction from positioned text boxes (OCR lines or PDF lines).

OCR engines return lines in rough top-to-bottom order, which interleaves the
label column and the value column of a form. This module rebuilds the form
structure geometrically, without assuming any particular template:

1. Header tables — a row of field labels with a row of values directly
   beneath (``DISTRICT | YEAR | P.S.`` over ``X | 2021 | Y``) become pairs.
2. Row anchors — short serial numbers ("01", "7") at the left edge of a
   visual row start a form row / list item.
3. Cells — vertically adjacent lines that overlap horizontally merge into one
   cell; cells are assigned to the anchor whose vertical extent they share.

Output rows have the same shape as PyMuPDF ``find_tables`` rows
(``[serial, label, value, ...]``) so one interpreter handles both.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from app.services.field_mapping_service import best_canonical_match

_ANCHOR = re.compile(r"^[\(\[]?\d{1,2}[\.\)\]]?$")
_GENERIC_DATE_LABEL = re.compile(r"^(?:date|dated|dt)\.?$", re.I)


@dataclass
class Box:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float = 1.0

    @property
    def h(self) -> float:
        return max(1.0, self.y1 - self.y0)

    @property
    def w(self) -> float:
        return max(1.0, self.x1 - self.x0)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class TableRow:
    """One table row: non-empty cell texts left to right."""

    page: int
    cells: List[str]
    origin: str  # pdf_table | ocr_layout
    bbox: Optional[Tuple[float, float, float, float]] = None


@dataclass
class HeaderPair:
    label: str
    value: str
    page: int
    order: int  # left-to-right position within its header table


@dataclass
class PageLayout:
    text: str
    rows: List[TableRow] = field(default_factory=list)
    header_pairs: List[HeaderPair] = field(default_factory=list)
    header_y: float = 0.0
    free_lines: List[Tuple[float, str]] = field(default_factory=list)  # (y, text) outside rows


@dataclass
class _Cell:
    boxes: List[Box]

    @property
    def x0(self) -> float:
        return min(b.x0 for b in self.boxes)

    @property
    def x1(self) -> float:
        return max(b.x1 for b in self.boxes)

    @property
    def y0(self) -> float:
        return min(b.y0 for b in self.boxes)

    @property
    def y1(self) -> float:
        return max(b.y1 for b in self.boxes)

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in sorted(self.boxes, key=lambda b: (b.cy, b.x0)))


def _x_overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _y_overlap(a: Box, b: Box) -> float:
    return max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))


def _visual_rows(boxes: Sequence[Box]) -> List[List[Box]]:
    """Cluster boxes that share a text baseline band."""
    rows: List[List[Box]] = []
    for box in sorted(boxes, key=lambda b: b.cy):
        for row in rows:
            ref = row[0]
            if _y_overlap(ref, box) >= 0.5 * min(ref.h, box.h):
                row.append(box)
                break
        else:
            rows.append([box])
    for row in rows:
        row.sort(key=lambda b: b.x0)
    rows.sort(key=lambda r: min(b.y0 for b in r))
    return rows


def _is_label_text(text: str) -> bool:
    t = text.strip().strip(":")
    if not t or len(t.split()) > 4:
        return False
    if _GENERIC_DATE_LABEL.match(t):
        return True
    canon, score = best_canonical_match(t)
    return canon is not None and score >= 0.85


def _header_pairs(boxes: List[Box], page: int, line_h: float) -> Tuple[List[HeaderPair], set]:
    """Find label rows with an aligned value row directly beneath."""
    pairs: List[HeaderPair] = []
    consumed: set = set()
    rows = _visual_rows([b for b in boxes if not _ANCHOR.match(b.text.strip())])
    for idx, row in enumerate(rows[:-1]):
        labels = [b for b in row if _is_label_text(b.text)]
        if len(labels) < 2 or len(labels) < 0.6 * len(row):
            continue
        below = rows[idx + 1]
        gap = min(b.y0 for b in below) - max(b.y1 for b in row)
        if gap > 1.5 * line_h:
            continue
        matched = []
        for order, lab in enumerate(labels):
            # Column span: halfway to neighbouring labels
            left = (labels[order - 1].x1 + lab.x0) / 2 if order > 0 else lab.x0 - lab.w
            right = (lab.x1 + labels[order + 1].x0) / 2 if order + 1 < len(labels) else lab.x1 + lab.w
            vals = [b for b in below if left <= b.cx <= right and id(b) not in consumed]
            if len(vals) == 1:
                matched.append((order, lab, vals[0]))
        if len(matched) < 2:
            continue
        for order, lab, val in matched:
            pairs.append(HeaderPair(lab.text.strip(), val.text.strip(), page, order))
            consumed.add(id(lab))
            consumed.add(id(val))
    return pairs, consumed


def _find_anchors(boxes: List[Box], line_h: float) -> List[Box]:
    anchors: List[Box] = []
    for box in boxes:
        if not _ANCHOR.match(box.text.strip()):
            continue
        same_row = [
            o for o in boxes
            if o is not box and abs(o.cy - box.cy) <= 1.2 * line_h + 0.5 * o.h
        ]
        has_right = any(o.x0 >= box.x1 - 2 for o in same_row)
        has_left = any(o.x1 <= box.x0 + 2 and _y_overlap(o, box) > 0 for o in same_row)
        if has_right and not has_left:
            anchors.append(box)
    anchors.sort(key=lambda b: b.y0)
    return anchors


def _anchor_of(box: Box, anchors: List[Box], line_h: float) -> Optional[int]:
    """Index of the anchor this box shares a vertical band with, if any."""
    best, best_ov = None, 0.2 * line_h
    for i, a in enumerate(anchors):
        ov = min(a.y1, box.y1) - max(a.y0, box.y0)
        if ov > best_ov:
            best, best_ov = i, ov
    return best


def _merge_cells(boxes: List[Box], anchors: List[Box], line_h: float) -> List[_Cell]:
    """Stack consecutive lines of one column into cells.

    Adjacency uses centre-to-centre distance (one line pitch) rather than box
    edges, because boxes from curved or skewed scans are taller than the
    line pitch and overlap their neighbours.
    """
    cells: List[_Cell] = []
    cell_anchor: List[Optional[int]] = []
    for box in sorted(boxes, key=lambda b: (b.cy, b.x0)):
        box_anchor = _anchor_of(box, anchors, line_h)
        best: Optional[int] = None
        best_dist = float("inf")
        for ci, cell in enumerate(cells):
            last = max(cell.boxes, key=lambda b: b.cy)
            pitch = box.cy - last.cy
            if pitch < 0.4 * line_h or pitch > 1.6 * line_h:
                continue
            ov = _x_overlap(cell.x0, cell.x1, box.x0, box.x1)
            if ov < 0.3 * min(cell.x1 - cell.x0, box.w):
                continue
            # Never merge lines that belong to different numbered rows
            if box_anchor is not None and cell_anchor[ci] is not None and box_anchor != cell_anchor[ci]:
                continue
            if any(cell.y0 + 0.3 * line_h < a.y0 <= box.y0 + 0.3 * line_h for a in anchors):
                if box_anchor is None or box_anchor != cell_anchor[ci]:
                    continue
            if pitch < best_dist:
                best, best_dist = ci, pitch
        if best is not None:
            cells[best].boxes.append(box)
            if cell_anchor[best] is None:
                cell_anchor[best] = box_anchor
        else:
            cells.append(_Cell([box]))
            cell_anchor.append(box_anchor)
    return cells


def _columns(cells: List[_Cell]) -> List[List[_Cell]]:
    cols: List[List[_Cell]] = []
    for cell in sorted(cells, key=lambda c: c.x0):
        for col in cols:
            if any(_x_overlap(c.x0, c.x1, cell.x0, cell.x1) > 0 for c in col):
                col.append(cell)
                break
        else:
            cols.append([cell])
    for col in cols:
        col.sort(key=lambda c: c.y0)
    cols.sort(key=lambda col: min(c.x0 for c in col))
    return cols


MIN_BOX_CONFIDENCE = 0.6  # lower scores are almost always bleed-through / specks


def layout_from_boxes(boxes: Sequence[Box], page: int, origin: str = "ocr_layout") -> PageLayout:
    boxes = [b for b in boxes if b.text and b.text.strip() and b.conf >= MIN_BOX_CONFIDENCE]
    if not boxes:
        return PageLayout(text="")
    line_h = statistics.median(b.h for b in boxes)

    pairs, consumed = _header_pairs(list(boxes), page, line_h)
    remaining = [b for b in boxes if id(b) not in consumed]
    anchors = _find_anchors(remaining, line_h)
    anchor_ids = {id(a) for a in anchors}
    cells = _merge_cells([b for b in remaining if id(b) not in anchor_ids], anchors, line_h)

    # Assign cells to anchors (row bands)
    bands: List[List[_Cell]] = [[] for _ in anchors]
    free: List[_Cell] = []
    for cell in sorted(cells, key=lambda c: c.y0):
        overlaps = [
            (min(a.y1, cell.y1) - max(a.y0, cell.y0), i) for i, a in enumerate(anchors)
        ]
        overlaps = [(ov, i) for ov, i in overlaps if ov > 0.2 * line_h]
        if overlaps:
            bands[max(overlaps)[1]].append(cell)
            continue
        # A cell that misses the anchor joins the band only when it shares a
        # row with a cell already there (value beside a tall label), or
        # continues a same-column cell (a second accused in one value cell).
        prev = [i for i, a in enumerate(anchors) if a.y0 <= cell.y0]
        if prev:
            i = prev[-1]
            nxt = anchors[i + 1].y0 if i + 1 < len(anchors) else float("inf")
            beside = [
                c for c in bands[i]
                if min(c.y1, cell.y1) - max(c.y0, cell.y0) > 0.2 * line_h
            ]
            above = [
                c for c in bands[i]
                if _x_overlap(c.x0, c.x1, cell.x0, cell.x1) > 0.3 * min(c.x1 - c.x0, cell.x1 - cell.x0)
            ]
            continues = above and cell.y0 - max(c.y1 for c in above) <= 1.8 * line_h
            if cell.y0 < nxt and (beside or continues):
                bands[i].append(cell)
                continue
        free.append(cell)

    rows: List[TableRow] = []
    elements: List[Tuple[float, str]] = []
    for anchor, band in zip(anchors, bands):
        cols = _columns(band)
        col_texts = ["\n".join(c.text for c in col) for col in cols]
        all_y = [anchor.y0] + [c.y0 for c in band]
        all_y1 = [anchor.y1] + [c.y1 for c in band]
        all_x0 = [anchor.x0] + [c.x0 for c in band]
        all_x1 = [anchor.x1] + [c.x1 for c in band]
        rows.append(
            TableRow(
                page=page,
                cells=[anchor.text.strip()] + col_texts,
                origin=origin,
                bbox=(min(all_x0), min(all_y), max(all_x1), max(all_y1)),
            )
        )
        head = anchor.text.strip()
        body = "\n".join(col_texts)
        elements.append((min(all_y), f"{head} {body}".strip()))
    free_lines: List[Tuple[float, str]] = []
    for cell in sorted(free, key=lambda c: c.y0):
        elements.append((cell.y0, cell.text))
        for b in sorted(cell.boxes, key=lambda b: b.cy):
            free_lines.append((b.y0, b.text))
    header_y = min((b.y0 for b in boxes if id(b) in consumed), default=0.0)
    if pairs:
        elements.append((header_y, "\n".join(f"{p.label}: {p.value}" for p in pairs)))
    elements.sort(key=lambda e: e[0])
    return PageLayout(
        text="\n".join(t for _, t in elements if t.strip()),
        rows=rows,
        header_pairs=pairs,
        header_y=header_y,
        free_lines=free_lines,
    )


def header_pairs_from_boxes(boxes: Sequence[Box], page: int) -> Tuple[List[HeaderPair], set]:
    """Header label-row / value-row tables among positioned native PDF lines."""
    boxes = [b for b in boxes if b.text and b.text.strip()]
    if len(boxes) < 4:
        return [], set()
    line_h = statistics.median(b.h for b in boxes)
    return _header_pairs(list(boxes), page, line_h)


_PUNCT_ONLY = re.compile(r"^[\s:;.,\-–—_]*$")


def rows_from_pdf_table(
    page: int, rows: Sequence[Sequence[str]], tops: Sequence[float]
) -> List[Tuple[TableRow, float]]:
    """find_tables rows → TableRows (empty/punctuation-only cells dropped) with their y."""
    out: List[Tuple[TableRow, float]] = []
    for i, raw in enumerate(rows):
        cells = [(c or "").strip() for c in raw]
        cells = [c for c in cells if not _PUNCT_ONLY.match(c)]
        if cells:
            y = tops[i] if i < len(tops) else (tops[-1] if tops else 0.0)
            out.append((TableRow(page=page, cells=cells, origin="pdf_table"), y))
    return out
