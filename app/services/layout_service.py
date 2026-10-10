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

import math
import re
import statistics
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from app.services.field_mapping_service import best_canonical_match
from app.services.field_synonyms import NARRATIVE_MARKERS
from app.services.text_normalize import compact

_ANCHOR = re.compile(r"^[\(\[]?\d{1,2}[\.\)\]]?$")
_GENERIC_DATE_LABEL = re.compile(r"^(?:date|dated|dt)\.?$", re.I)


def is_narrative_marker(text: str) -> bool:
    """'Brief facts of the case', 'Honoured Sir,', 'Facts of the case are that ...'."""
    comp = compact(text)
    return bool(comp) and any(re.search(p, comp) for p in NARRATIVE_MARKERS)


def is_court_heading(text: str) -> bool:
    """'IN THE COURT OF ...' / 'IN THE HON'BLE ... MAGISTRATE COURT' — the heading of the report to the court."""
    comp = compact(text)
    return comp.startswith("inthe") and "court" in comp[:90]


def _starts_section(text: str) -> bool:
    """Text that begins something new below the form rows; never a continuation of a row."""
    return is_narrative_marker(text) or is_court_heading(text)


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
    if not t or len(t.split()) > 4 or re.search(r":\s*\S", t):
        return False  # 'Year: 2021' carries its own value
    if _GENERIC_DATE_LABEL.match(t):
        return True
    canon, score = best_canonical_match(t)
    return canon is not None and score >= 0.85


def _header_pairs(boxes: List[Box], page: int, line_h: float) -> Tuple[List[HeaderPair], set]:
    """Find label rows with an aligned value row directly beneath."""
    pairs: List[HeaderPair] = []
    consumed: set = set()
    serials = [b for b in boxes if _ANCHOR.match(b.text.strip())]
    rows = _visual_rows([b for b in boxes if not _ANCHOR.match(b.text.strip())])
    for idx, row in enumerate(rows[:-1]):
        if any(_y_overlap(s, b) > 0.3 * min(s.h, b.h) and s.x1 <= b.x0 + 2 for s in serials for b in row[:1]):
            continue  # a numbered form row ('05 | Type of final result | Charge sheet'), not a header table
        labels = [b for b in row if _is_label_text(b.text)]
        # Cells that carry their own value ('Year: 2021') still make the row a label row
        self_pairs = [b for b in row if re.search(r":\s*\S", b.text) and _is_label_text(b.text.split(":", 1)[0])]
        if len(labels) < 2 or len(labels) + len(self_pairs) < 0.6 * len(row):
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


def _spans_past(item, cell) -> bool:
    """True when ``item`` is a page-wide line rather than more of ``cell``'s column.

    Lines of one column stay inside it; a heading or narrative line under the
    last form row is far wider than a label cell and runs a full cell width
    past its right edge into the next column.
    """
    cell_w = cell.x1 - cell.x0
    return (item.x1 - item.x0) > 2.5 * cell_w and item.x1 > cell.x1 + cell_w


def _core_overlap(cell: "_Cell", y0: float, y1: float, line_h: float) -> float:
    """Vertical overlap of [y0, y1] with the text cores of a cell's lines.

    A curved or tilted line gets a box much taller than the text; only one
    line height around the box centre is text, so the padding must not reach
    into the row above.
    """
    best = 0.0
    for b in cell.boxes:
        core0, core1 = max(b.y0, b.cy - line_h / 2), min(b.y1, b.cy + line_h / 2)
        best = max(best, min(y1, core1) - max(y0, core0))
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
        # A narrative start ("Brief facts of the case", "Honoured Sir,") begins its own cell
        starts_narrative = _starts_section(box.text)
        for ci, cell in enumerate(cells if not starts_narrative else []):
            last = max(cell.boxes, key=lambda b: b.cy)
            pitch = box.cy - last.cy
            if pitch < 0.4 * line_h or pitch > 1.6 * line_h:
                continue
            ov = _x_overlap(cell.x0, cell.x1, box.x0, box.x1)
            if ov < 0.3 * min(cell.x1 - cell.x0, box.w):
                continue
            if _spans_past(box, cell):
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
            top_cy = min(b.cy for b in cells[best].boxes)
            # A cell that starts clearly above a row (a centred title) never adopts that row
            if cell_anchor[best] is None and (box_anchor is None or top_cy >= anchors[box_anchor].y0 - 0.5 * line_h):
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
MIN_SKEW_DEGREES = 0.3  # below this the page is treated as straight
MAX_LINE_DEVIATION_DEGREES = 20.0  # text rotated this far from the page is bleed-through / stamps
COLUMN_STRIPS = 6  # vertical strips of the page whose offsets are measured separately
MIN_STRIP_PAIRS = 4  # fewer aligned pairs than this: too little evidence to move a strip
COLUMN_OFFSET_MIN_GAIN = 1.5  # an offset must line up 50% more pairs than keeping the previous one


def _poly_angle(poly: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    """(angle in degrees of the line's long edge, length of that edge)."""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = poly
    top = (x1 - x0, y1 - y0)
    left = (x3 - x0, y3 - y0)
    edge = top if math.hypot(*top) >= math.hypot(*left) else (y3 - y0, -(x3 - x0))  # tall box: use side
    return math.degrees(math.atan2(edge[1], edge[0])), math.hypot(*edge)


def page_skew(polys: Sequence[Optional[Sequence[Tuple[float, float]]]]) -> float:
    """Median angle of the long text lines on a page (degrees, image y axis pointing down)."""
    angles = []
    for poly in polys:
        if not poly:
            continue
        angle, length = _poly_angle(poly)
        if length >= 150 and abs(angle) < 30:
            angles.append(angle)
    return statistics.median(angles) if len(angles) >= 3 else 0.0


def column_offsets(boxes: Sequence[Box], width: float, line_h: float) -> List[float]:
    """Vertical offset of each vertical strip of the page relative to the strips to its left.

    On photocopies of a tilted or warped form the value column often sits lower
    (or higher) than the label column — sometimes by a whole row — while each text
    line stays straight, so no single rotation fixes it. The page is cut into
    ``COLUMN_STRIPS`` strips by left edge; going left to right, each strip gets the
    offset that best lines its boxes up with boxes further left (label | value
    pairs on one row). An offset is used only with clear evidence; otherwise the
    strip keeps the offset of the strip before it (0 on a straight page).
    """
    step = width / COLUMN_STRIPS
    strip_of = [min(COLUMN_STRIPS - 1, max(0, int(b.x0 // step))) for b in boxes]
    offsets = [0.0] * COLUMN_STRIPS
    candidates = [k * line_h / 8 for k in range(-16, 17)]
    for s in range(1, COLUMN_STRIPS):
        right = [b for b, k in zip(boxes, strip_of) if k == s]
        left = [(b, offsets[k]) for b, k in zip(boxes, strip_of) if k < s]
        if not right or not left:
            offsets[s] = offsets[s - 1]
            continue

        def aligned(d: float) -> int:
            return sum(
                1 for r in right for lb, ld in left
                if r.x0 > lb.x1 and abs((r.cy - d) - (lb.cy - ld)) < 0.3 * line_h
            )

        scores = {d: aligned(d) for d in candidates}
        # Every offset within the alignment tolerance scores the same: take the middle one
        top = max(scores.values())
        tied = [d for d in candidates if scores[d] == top]
        best = tied[len(tied) // 2]
        base = max(scores[offsets[s - 1]] if offsets[s - 1] in scores else scores[0.0], scores[0.0], 1)
        if scores[best] >= MIN_STRIP_PAIRS and scores[best] >= COLUMN_OFFSET_MIN_GAIN * base:
            offsets[s] = best
        else:
            offsets[s] = offsets[s - 1]
    return offsets


def reading_order(bboxes: Sequence[Tuple[float, float, float, float]], skew_deg: float = 0.0) -> List[int]:
    """Indices of boxes top to bottom, left to right within a text row, in a straightened frame."""
    if not bboxes:
        return []
    s, c = math.sin(math.radians(skew_deg)), math.cos(math.radians(skew_deg))
    centers = [((x0 + x1) / 2, (y0 + y1) / 2) for x0, y0, x1, y1 in bboxes]
    ys = [cy * c - cx * s for cx, cy in centers]
    xs = [cx * c + cy * s for cx, cy in centers]
    half_line = 0.5 * statistics.median(max(y1 - y0, 1.0) for _, y0, _, y1 in bboxes)
    rows: List[Tuple[float, List[int]]] = []
    for i in sorted(range(len(bboxes)), key=lambda k: ys[k]):
        if rows and ys[i] - rows[-1][0] < half_line:
            rows[-1][1].append(i)
        else:
            rows.append((ys[i], [i]))
    return [i for _, members in rows for i in sorted(members, key=lambda k: xs[k])]


def boxes_from_ocr(lines: Sequence, width: float, height: float) -> Tuple[List[Box], float]:
    """OCR lines → axis-aligned boxes in a straightened page frame.

    A photographed page is often rotated a few degrees; a 3° tilt moves the right
    end of a row ~30 px up and puts values into the previous row. When the OCR
    engine returns line polygons, the page angle is measured from them and every
    line is rotated back. A column that still sits lower or higher than the text
    to its left (tilted or warped photocopy) is moved back by its measured offset
    (``column_offsets``) before layout analysis.
    Lines rotated far from the page angle (text bleeding through from the facing
    page, stamps) are dropped. Returns (boxes, skew_degrees).
    """
    polys = [getattr(l, "poly", None) for l in lines]
    skew = page_skew(polys)
    kept = []
    for line in lines:
        poly = getattr(line, "poly", None)
        if poly:
            angle, length = _poly_angle(poly)
            if length >= 40 and abs(angle - skew) > MAX_LINE_DEVIATION_DEGREES:
                continue
        kept.append(line)

    cx, cy = width / 2, height / 2

    def rotated(deg: float) -> List[Box]:
        theta = math.radians(deg) if abs(deg) >= MIN_SKEW_DEGREES else 0.0
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        out: List[Box] = []
        for line in kept:
            if not theta:
                out.append(Box(line.text, *line.bbox, conf=line.confidence))
                continue
            x0, y0, x1, y1 = line.bbox
            corners = getattr(line, "poly", None) or [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            pts = [(cx + (x - cx) * cos_t + (y - cy) * sin_t, cy - (x - cx) * sin_t + (y - cy) * cos_t) for x, y in corners]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            out.append(Box(line.text, min(xs), min(ys), max(xs), max(ys), conf=line.confidence))
        return out

    boxes = rotated(skew)
    confident = [b for b in boxes if b.conf >= MIN_BOX_CONFIDENCE and b.text.strip()]
    if len(confident) >= 6:
        offsets = column_offsets(confident, width, statistics.median(b.h for b in confident))
        if any(offsets):
            step = width / COLUMN_STRIPS
            moved = []
            for b in boxes:
                d = offsets[min(COLUMN_STRIPS - 1, max(0, int(b.x0 // step)))]
                moved.append(Box(b.text, b.x0, b.y0 - d, b.x1, b.y1 - d, conf=b.conf))
            boxes = moved
    return boxes, round(skew if abs(skew) >= MIN_SKEW_DEGREES else 0.0, 2)


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
    narrative_y: Optional[float] = None
    for cell in sorted(cells, key=lambda c: c.y0):
        top_cy = min(b.cy for b in cell.boxes)
        # A cell that starts clearly above a row (a centred title over the table) is not part of it
        overlaps = [
            (_core_overlap(cell, a.y0, a.y1, line_h), i) for i, a in enumerate(anchors)
            if top_cy >= a.y0 - 0.5 * line_h
        ]
        overlaps = [(ov, i) for ov, i in overlaps if ov > 0.2 * line_h]
        if overlaps:
            bands[max(overlaps)[1]].append(cell)
            continue
        # Once the narrative starts ("Brief facts of the case", "Honoured Sir,")
        # nothing below it continues the form row above — when serial numbers
        # are missed, continuation would otherwise chain the narrative into it.
        if narrative_y is None and _starts_section(min(cell.boxes, key=lambda b: b.cy).text):
            narrative_y = cell.y0
        # A cell that misses the anchor joins the band only when it shares a
        # row with a cell already there (value beside a tall label), or
        # continues a same-column cell (a second accused in one value cell).
        prev = [i for i, a in enumerate(anchors) if a.y0 <= cell.y0]
        if prev and not (narrative_y is not None and anchors[prev[-1]].y0 < narrative_y):
            i = prev[-1]
            nxt = anchors[i + 1].y0 if i + 1 < len(anchors) else float("inf")
            beside = [c for c in bands[i] if _core_overlap(cell, c.y0, c.y1, line_h) > 0.2 * line_h]
            above = [
                c for c in bands[i]
                if _x_overlap(c.x0, c.x1, cell.x0, cell.x1) > 0.3 * min(c.x1 - c.x0, cell.x1 - cell.x0)
                and not _spans_past(cell, c)
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
