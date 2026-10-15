"""Positioned document model and label helpers shared by the field builder.

A page is a list of ``DocItem``s in reading order: form table rows, header
table label/value pairs, and free text lines, built from either native PDF
text or OCR. The helpers here decide whether a piece of text is a *label*;
they never rename or rewrite text. The vocabulary in ``field_synonyms`` is
used only to recognise that a phrase is a label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from app.services.field_mapping_service import (
    best_canonical_match,
    is_generic_date_label,
    list_heading_kind,
)
from app.services.layout_service import HeaderPair, TableRow, is_narrative_marker
from app.services.text_normalize import clean_value, compact, soften_pdf_text
from app.services.value_parsers import find_numeric_date

SERIAL = re.compile(r"^[\(\[]?0?(\d{1,2})[.)\]]?$")
LEADING_SERIAL = re.compile(r"^\s*[\(\[]?(0?\d{1,2})\s*[.)\]]?\s+(?=\S)")
_COLON_LABEL = re.compile(r"(?:(?<=^)|(?<=[\s,;]))([A-Za-z][A-Za-z.&/()' ]{0,45}?)\s*:\s*")

# Vocabulary entries for people: "Sri X" must not be split into a label + value
PERSON_FIELDS = {"io_name", "complainant_name", "informant_name", "accused_name", "witness_name"}


@dataclass
class DocItem:
    """One positioned element of a page in reading order."""

    page: int
    y: float
    text: str
    row: Optional[TableRow] = None
    header: Optional[HeaderPair] = None


# Watermarks stamped by scanner apps ("Scanned with CamScanner", "Scanned by X")
_SCANNER_MARK = re.compile(r"^(?:scanned|digiti[sz]ed)(?:with|by|using)[a-z0-9]{2,24}$")


def drop_page_furniture(pages: Sequence[Sequence[DocItem]]) -> Tuple[List[List[DocItem]], int]:
    """Remove scanner watermarks and short header/footer lines repeated across pages.

    A line counts as page furniture when it is a scanner-app watermark, or when
    the same short line is the first or last free line on two or more pages.
    Form rows and header tables are never touched. Returns (pages, lines dropped).
    """
    edge_count: Dict[str, int] = {}
    for page in pages:
        free = [it for it in page if it.row is None and it.header is None]
        keys = {compact(it.text) for it in free[:1] + free[-1:] if len(it.text.split()) <= 8}
        for key in keys:
            if len(key) >= 6:
                edge_count[key] = edge_count.get(key, 0) + 1
    repeated = {k for k, n in edge_count.items() if n >= 2}
    out: List[List[DocItem]] = []
    dropped = 0
    for page in pages:
        kept = []
        for it in page:
            key = compact(it.text) if it.row is None and it.header is None else ""
            if key and (_SCANNER_MARK.match(key) or key in repeated):
                dropped += 1
                continue
            kept.append(it)
        out.append(kept)
    return out, dropped


# ---------------------------------------------------------------------------
# Label recognition
# ---------------------------------------------------------------------------


def recognise(label: str) -> Tuple[Optional[str], float]:
    """Vocabulary entry for a label (recognition only), ('date', 1.0) for 'Date'/'Dated'/'On'."""
    if is_generic_date_label(label):
        return "date", 1.0
    return best_canonical_match(label, include_hints=True)


def _label_like(label: str) -> bool:
    """Abbreviated labels ('Dist.', 'FIR. No.', 'P.S.', 'PS') — not prose words."""
    return label.endswith(".") or bool(re.fullmatch(r"[A-Z.]{2,6}", label))


def _prefix_label(part: str) -> Optional[Tuple[str, str]]:
    """Split 'FIR. No. 4/2017' / 'Dist. Northtown' / 'On 02.03.2017' into (label, value).

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
        if not clean_value(value) or not label[0].isalpha() or re.fullmatch(r"[A-Za-z]/[A-Za-z]{1,2}\.?", label):
            continue  # 'U/s 20(b)', 'r/w 22' are part of a value, not labels
        if k == 1 and is_generic_date_label(label):
            shaped = find_numeric_date(value)
            if shaped and shaped.start() == 0:
                return label, value  # 'On 02.03.2017 at 1500 hrs' — the value starts with the date
            continue
        if not (_label_like(label) or value[:1].isdigit()):
            continue
        canon, score = best_canonical_match(label)
        if canon and score >= 0.9 and canon not in PERSON_FIELDS and (best is None or score > best[2]):
            best = (label, value, score)
    return (best[0], best[1]) if best else None


def strip_serial(line: str) -> Tuple[Optional[str], str]:
    """'02. Charge Sheet No.' → ('02', 'Charge Sheet No.')."""
    m = LEADING_SERIAL.match(line)
    if not m:
        return None, line
    return m.group(1), line[m.end():].strip()


def parse_cell_pairs(text: str) -> Tuple[List[Tuple[str, str]], str]:
    """'Label: value' and 'Label value' pairs inside one cell / line.

    Returns (pairs, leftover): pairs are (label, value) as printed; leftover is
    the text before the first recognised label. A line without a label after
    a pair continues that pair's value.
    """
    pairs: List[Tuple[str, str]] = []
    leftovers: List[str] = []
    for raw_line in (text or "").split("\n"):
        line = strip_serial(raw_line.strip())[1]
        if not line:
            continue
        line_pairs, rest = _line_pairs(line)
        if rest and pairs:
            label, value = pairs[-1]
            pairs[-1] = (label, f"{value}\n{rest}".strip())
        elif rest:
            leftovers.append(rest)
        pairs.extend(line_pairs)
    return pairs, "\n".join(leftovers).strip()


def _line_pairs(line: str) -> Tuple[List[Tuple[str, str]], str]:
    """Pairs on one line, and the text before its first label."""
    starts = []
    for m in _COLON_LABEL.finditer(line):
        label, begin = m.group(1), m.start(1)
        canon, score = recognise(label.strip())
        cut = max(label.rfind(". "), label.rfind(", "), label.rfind("; "))
        tail = label[cut + 2:].strip() if cut >= 0 else ""
        if tail[:1].isalpha() and not is_generic_date_label(tail):
            t_canon, t_score = recognise(tail)
            if t_canon and t_score >= score:
                # 'Northtown. Police Station:' — the label starts after the previous value ends
                begin += cut + 2
                label, canon, score = tail, t_canon, t_score
        label = label.strip()
        if canon and score >= 0.85:
            starts.append((begin, m.end(), label))
    segments: List[Tuple[Optional[str], str]] = []
    if not starts:
        segments.append((None, line))
    else:
        if starts[0][0] > 0:
            segments.append((None, line[: starts[0][0]]))
        for idx, (_, end_label, label) in enumerate(starts):
            end = starts[idx + 1][0] if idx + 1 < len(starts) else len(line)
            segments.append((label, line[end_label:end]))
    pairs: List[Tuple[str, str]] = []
    leftovers: List[str] = []
    for head, body in segments:
        current = head
        values: List[str] = []
        for part in re.split(r"(?<=[,;])\s+", body):
            pref = _prefix_label(part.rstrip(",;"))
            if pref is None:
                values.append(part)
                continue
            if current is not None:
                pairs.append((current, " ".join(values).strip().rstrip(",;")))
            elif values:
                leftovers.append(" ".join(values).strip())
            current, values = pref[0], [pref[1]]
        if current is not None:
            pairs.append((current, " ".join(values).strip().rstrip(",;")))
        elif values and clean_value(" ".join(values)):
            leftovers.append(" ".join(values).strip())
    return pairs, " ".join(leftovers).strip()

def _inline_label_len(tokens: List[str], i: int, first: bool) -> int:
    """Number of tokens of a label printed at ``tokens[i]`` without a colon, or 0.

    The label must be a vocabulary label printed as is (near-exact match, never a person field) and
    must have a value after it on its line; after the first label of a cell it must start with a
    capital letter, so ordinary words inside a value ('investigation', 'court') are not labels.
    """
    head = tokens[i]
    if not head[:1].isalpha() or (not first and not head[:1].isupper()):
        return 0
    best_k, best_score = 0, 0.0
    for k in range(min(8, len(tokens) - i - 1), 0, -1):  # the best match wins; a longer label wins a tie
        label, value = " ".join(tokens[i:i + k]), " ".join(tokens[i + k:])
        if is_generic_date_label(label):
            shaped = find_numeric_date(value)
            score = 1.0 if shaped and shaped.start() == 0 else 0.0  # 'Dated 24-10-2024'
        else:
            canon, score = best_canonical_match(label)
            if not canon or canon in PERSON_FIELDS:
                score = 0.0
        if score >= 0.95 and score > best_score:
            best_k, best_score = k, score
    return best_k


def inline_label_pairs(text: str) -> List[Tuple[str, str]]:
    """'District X Police Station Y Year 2024' / 'Cr. No. 498/2024 Dated 24-10-2024' - several labels
    printed in one cell without colons, each followed by its value.

    For a cell ``parse_cell_pairs`` cannot split. The cell must start with a label and hold at least
    two; otherwise nothing is returned. The word right after a label always belongs to its value, and
    a line that does not start with a label continues the previous value.
    """
    pairs: List[List[str]] = []
    for raw_line in (text or "").split("\n"):
        tokens = raw_line.split()
        i, after_label = 0, False
        while i < len(tokens):
            k = 0 if after_label else _inline_label_len(tokens, i, first=not pairs)
            if k:
                pairs.append([" ".join(tokens[i:i + k]), ""])
                i, after_label = i + k, True
                continue
            if not pairs:
                return []  # the cell does not start with a label
            sep = " " if pairs[-1][1] and i > 0 else ("\n" if pairs[-1][1] else "")
            pairs[-1][1] += sep + tokens[i]
            i, after_label = i + 1, False
    if len(pairs) < 2:
        return []
    return [(label, value.strip().rstrip(",;")) for label, value in pairs]


def colon_label(line: str) -> Optional[Tuple[str, str]]:
    """'Some Label : value' with a short label that reads like one (not a sentence)."""
    if ":" not in line:
        return None
    label, value = line.split(":", 1)
    label = label.strip()
    words = label.split()
    if not label or len(label) > 60 or len(words) > 8 or not label[0].isalpha():
        return None
    if re.search(r"(?i)\b(?:s/o|d/o|w/o|r/o|age|caste|occ)\b", label) and recognise(label)[1] < 0.9:
        return None  # person details ("Sri X S/o Y, Age") are values, not labels
    if re.search(r"[.!?]\s", label):
        return None  # a sentence before the colon
    return label, value.strip()


def is_heading(text: str) -> bool:
    """A label printed on its own line, its value below ('Particulars of the accused')."""
    t = text.strip().rstrip(":").strip()
    words = t.split()
    if not t or len(words) > 12 or re.search(r"(?i)\b(?:s/o|d/o|w/o|r/o)\b", t):
        return False
    if text.strip().endswith(":") and len(words) <= 8 and t[0].isalpha():
        return True
    if list_heading_kind(t):
        return True
    canon, score = recognise(t)
    # One word ("IPC", "Nil") is a value far more often than a label on its own line
    return canon is not None and score >= 0.88 and len(words) >= 2 and t[0].isalpha()


def row_body(row: TableRow) -> Tuple[Optional[str], List[str]]:
    """(serial printed in the first column, remaining non-empty cells)."""
    cells = [soften_pdf_text(c).strip() for c in row.cells if c and c.strip()]
    if cells and SERIAL.match(cells[0]):
        return cells[0], cells[1:]
    return None, cells


# ---------------------------------------------------------------------------
# Narrative start
# ---------------------------------------------------------------------------


def _is_prose(text: str) -> bool:
    words = text.split()
    return len(words) >= 10 and len(text) >= 70 and bool(re.search(r"[a-z]{3,}\s+[a-z]{2,}\s+[a-z]{2,}", text))


def find_narrative_start(items: Sequence[DocItem]) -> Optional[int]:
    """Index of the item where the running narrative begins, or None."""
    for i, item in enumerate(items):
        if item.row is not None:
            cells = row_body(item.row)[1]
            # The label cell can also hold the first narrative line when a wide
            # heading ("IN THE COURT OF ...") starts inside the label column.
            if cells and "brief_facts" in (
                best_canonical_match(cells[0])[0],
                best_canonical_match(cells[0].split("\n", 1)[0])[0],
            ):
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
