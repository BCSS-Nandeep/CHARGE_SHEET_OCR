"""What the report itself shows for each option, and the VLM-friendly layout of choice tables.

Checkbox-heavy tables are printed column by column, but a markdown table is written row by
row, which is hard for a small VLM to read back column-wise. This module parses a section's
tables once and uses that parse for two things:

* ``EvidenceIndex.restructured`` - the section text with each choice table rewritten as one
  ``COLUMN <header>`` (or ``ROW <label>``) block per printed column / row, options in their
  original order, every line keeping its ``☑ / ☐`` box, ``**bold**`` and written "Yes".
* ``EvidenceIndex.marks_for / text_for`` - per field, the options found in that column or row,
  each with separate attributes (``box``, ``bold``, ``yes``) and its coordinate, which the
  validator uses to check the model's answer against the source.

A bold option or a written "Yes" is recorded as its own attribute; neither is treated as a
selection by this module.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

from app.ir_standard.matching import _norm, is_blank, snap_option
from app.ir_standard.schema import MULTI, SINGLE, FieldSpec, SectionSpec

TICK, BOX = "☑", "☐"
_TICK_CHARS = "☑☒✓✔"  # ☑ ☒ ✓ ✔ all mean "checked"
_BOX_CHARS = "☐□"
_YES_SUFFIX = re.compile(r"^(?P<label>.*?[^\s:;,\-–(])[\s:;,\-–(]*\b(?:yes|yeas|yess)\b\W*$", re.IGNORECASE)
_HEADER_MATCH = 0.84


@dataclass(frozen=True)
class Mark:
    """One printed option with the evidence attached to it (attributes are independent)."""

    label: str  # option text with box glyphs, bold markers and a trailing "Yes" removed
    raw: str  # the line exactly as read
    box: str  # ticked | empty | none
    bold: bool
    yes: bool  # a written "Yes" after the label (not an option called "Yes")
    at: str  # coordinate: table / block / column / row / option line

    @property
    def state(self) -> str:
        """selected = ticked box; uncertain = bold and/or "Yes" only; unselected = nothing marks it."""
        if self.box == "ticked":
            return "selected"
        if self.bold or self.yes:
            return "uncertain"
        return "unselected"


def parse_mark(raw: str, at: str = "") -> Mark:
    s = raw.replace(" ", " ").strip()
    box = "ticked" if any(c in s for c in _TICK_CHARS) else ("empty" if any(c in s for c in _BOX_CHARS) else "none")
    # bold only counts when a bold span holds actual text (a bold tick symbol alone is not bold text)
    bold = any(re.search(r"\w", span) for span in re.findall(r"\*\*(.+?)\*\*", s))
    label = re.sub(f"[{_TICK_CHARS}{_BOX_CHARS}]", " ", s.replace("**", ""))
    label = re.sub(r"\s+", " ", label).strip(" :;-–")
    yes = False
    m = _YES_SUFFIX.match(label)
    if m:  # "Black: Yes" -> label Black + yes; a bare "Yes" is an option, not a marker
        label, yes = m.group("label").strip(" :;-–"), True
    return Mark(label, s, box, bold, yes, at)


def clean_cell(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.replace("<br>", " ").replace("**", "")).strip()


def cell_lines(cell: str) -> List[str]:
    return [p.strip() for p in cell.split("<br>") if p.strip()]


def merge_wrapped(lines: List[str], f: Optional[FieldSpec]) -> List[str]:
    """Re-join an option Word wrapped onto a second paragraph ("☐ Hunch" + "Back" -> "☐ Hunch Back").

    Only when the next line is a continuation (no box of its own, not an option by itself) and the
    two lines together are a canonical option of ``f``.
    """
    if f is None or f.kind not in (SINGLE, MULTI):
        return lines
    out: List[str] = []
    i = 0
    while i < len(lines):
        cur = lines[i]
        if i + 1 < len(lines):
            nxt = parse_mark(lines[i + 1])
            if nxt.box == "none" and snap_option(nxt.label, f.options) is None:
                joined = cur + " " + lines[i + 1]
                if snap_option(parse_mark(joined).label, f.options) is not None:
                    out.append(joined)
                    i += 2
                    continue
        out.append(cur)
        i += 1
    return out


# ---------------------------------------------------------------------------
# HTML tables (the VLM may answer with <table>) -> markdown tables
# ---------------------------------------------------------------------------

class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[str]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None
        self._span = 1

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
            self._span = 1
            for k, v in attrs:
                if k == "colspan" and (v or "").isdigit():
                    self._span = max(1, min(int(v), 12))
        elif tag == "br" and self._cell is not None:
            self._cell.append("<br>")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            text = re.sub(r"[ \t\r\n]+", " ", "".join(self._cell)).replace("|", "/").strip()
            self._row.append(text)
            self._row.extend([""] * (self._span - 1))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def html_tables_to_markdown(text: str) -> str:
    def convert(match: "re.Match[str]") -> str:
        parser = _TableParser()
        parser.feed(match.group(0))
        rows = [r for r in parser.rows if r]
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        lines = []
        for i, r in enumerate(rows):
            r = r + [""] * (width - len(r))
            lines.append("| " + " | ".join(r) + " |")
            if i == 0:
                lines.append("|" + " --- |" * width)
        return "\n" + "\n".join(lines) + "\n"

    return re.sub(r"<table\b.*?</table>", convert, text, flags=re.IGNORECASE | re.DOTALL)


# ---------------------------------------------------------------------------
# matching table headers / row labels to template fields
# ---------------------------------------------------------------------------

def match_field(text: str, fields: Tuple[FieldSpec, ...]) -> Optional[FieldSpec]:
    """The field a header / row-label cell names: by printed column number "(5)", else by label."""
    t = clean_cell(re.sub(f"[{_TICK_CHARS}{_BOX_CHARS}]", "", text))
    if not t:
        return None
    m = re.search(r"\((\d{1,2})\)", t)
    if m:
        for f in fields:
            if f.column_no == int(m.group(1)):
                return f
    core = _norm(re.sub(r"\(.*?\)", "", t))
    if len(core) < 3:
        return None
    best: Optional[Tuple[float, FieldSpec]] = None
    for f in fields:
        label = _norm(f.label)
        if core == label:
            return f
        if len(core) < 5:  # fuzzy matching on very short words is how the word "yes" became the column "Eyes"
            continue
        r = difflib.SequenceMatcher(None, core, label).ratio()
        if r >= _HEADER_MATCH and (best is None or r > best[0]):
            best = (r, f)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------

class EvidenceIndex:
    """Parsed view of one section's text: restructured prompt text + per-field evidence."""

    def __init__(self, spec: SectionSpec, chunk: str, source: str = "document"):
        self.spec = spec
        self.source = source
        self.fields = spec.fields
        self.choice_fields = [f for f in spec.fields if f.kind in (SINGLE, MULTI)]
        self._marks: Dict[str, List[Mark]] = {}
        self._texts: Dict[str, List[str]] = {}
        self._inline: Dict[str, List[Mark]] = {}
        self._loose: List[Mark] = []
        self._body: List[str] = []  # text the report really contains (table header rows excluded)
        self._body_tokens: Optional[set] = None
        self.restructured = self._build(chunk)

    # -- public ---------------------------------------------------------

    def has_context(self, f: FieldSpec) -> bool:
        return f.key in self._marks or f.key in self._texts

    def text_for(self, f: FieldSpec) -> Optional[str]:
        """What the report prints in the field's own column / row (None when it could not be located)."""
        parts = self._texts.get(f.key)
        return " ".join(parts) if parts is not None else None

    def marks_for(self, f: FieldSpec) -> Optional[List[Mark]]:
        """Every option printed for this field, with its evidence; None when the source could not be located."""
        if f.key in self._marks:
            return self._marks[f.key]
        if f.key in self._inline:
            return self._inline[f.key]
        mine = [m for m in self._loose if snap_option(m.label, f.options)]
        if not mine:
            return None
        # context-free options belong to this field only if no sibling choice field could claim them too
        for other in self.choice_fields:
            if other.key != f.key and any(snap_option(m.label, other.options) for m in mine):
                return None
        return mine

    # -- grounding: a value must come from the report ----------------------------------

    @staticmethod
    def _tokens(text: str) -> List[str]:
        return re.findall(r"[a-z0-9]+", text.lower())

    def is_grounded(self, value: object) -> bool:
        """True when every word / number of ``value`` appears in the section's own text.

        Header rows of tables do not count (a column title is not data), so "Member 1" is not grounded by
        a column called "Member name". Placeholder text such as ``<text or null>`` never is.
        """
        text = "" if value is None else str(value)
        if re.search(r"<[^<>]*(?:text|null|or|one of|any of)[^<>]*>", text, re.IGNORECASE):
            return False
        want = self._tokens(text)
        if not want:
            return True
        if self._body_tokens is None:
            self._body_tokens = {t for line in self._body for t in self._tokens(line)}
        have = self._body_tokens
        for t in want:
            if t in have:
                continue
            if len(t) >= 5 and difflib.get_close_matches(t, have, n=1, cutoff=0.86):  # a scan's misreading
                continue
            return False
        return True

    def written_answer(self, f: FieldSpec) -> Optional[str]:
        """The text the report writes for a choice field when it is NOT an option list.

        "Living status | Living with parents" is a written answer; "Living status | ☐ Alone / ☑ With Family"
        is an option list (two or more template options printed). None when the field is an option list or
        could not be located.
        """
        if f.kind not in (SINGLE, MULTI):
            return None
        text = self.text_for(f)
        if text is None:
            return None
        marks = self.marks_for(f) or []
        printed = {snap_option(m.label, f.options) for m in marks} - {None}
        if len(printed) >= 2 or any(m.box != "none" for m in marks):
            return None
        return text.strip()

    def dump(self) -> dict:
        """Everything the index found, for audit / evaluation output (no behaviour depends on it)."""
        fields: Dict[str, dict] = {}
        for f in self.fields:
            marks, text = self.marks_for(f), self.text_for(f)
            if marks is None and text is None:
                continue
            fields[f.key] = {
                "marks": None if marks is None else [
                    {"label": m.label, "raw": m.raw, "box": m.box, "bold": m.bold, "yes": m.yes,
                     "state": m.state, "at": m.at} for m in marks],
                "text": text,
            }
        return {"source": self.source, "restructured": self.restructured, "fields": fields}

    # -- parsing --------------------------------------------------------

    def _header_fields(self, row: List[str]) -> Dict[int, FieldSpec]:
        out: Dict[int, FieldSpec] = {}
        for ci, cell in enumerate(row):
            f = match_field(cell, self.fields)
            if f is not None:
                out[ci] = f
        return out

    @staticmethod
    def _is_header(hf: Dict[int, FieldSpec], row: List[str]) -> bool:
        nonempty = sum(1 for c in row if clean_cell(c))
        return len({f.key for f in hf.values()}) >= 2 and len(hf) * 2 >= nonempty

    def _add(self, f: Optional[FieldSpec], raw_line: str, at: str) -> None:
        mark = parse_mark(raw_line, at)
        if f is None:
            self._loose.append(mark)
            return
        self._marks.setdefault(f.key, []).append(mark)
        self._texts.setdefault(f.key, []).append(clean_cell(re.sub(f"[{_TICK_CHARS}{_BOX_CHARS}]", "", raw_line)))

    def _table(self, ti: int, rows: List[List[str]]) -> List[str]:
        """Index one table; return its lines for the prompt (column/row blocks when it is a choice table)."""
        blocks: List[Tuple[str, str, List[str]]] = []  # (kind, title, option lines) in table order
        choice_hits = 0
        loose_before = len(self._loose)
        current: Optional[Tuple[int, Dict[int, FieldSpec], List[str]]] = None
        col_lines: Dict[Tuple[int, int], List[str]] = {}
        order: List[Tuple[int, int, str, Optional[FieldSpec]]] = []  # (block, column, title, field)
        bi = -1
        for ri, row in enumerate(rows):
            hf = self._header_fields(row)
            if self._is_header(hf, row):
                bi += 1
                current = (bi, hf, row)
                for ci in range(len(row)):
                    title = clean_cell(row[ci])
                    if title:
                        order.append((bi, ci, title, hf.get(ci)))
                        col_lines[(bi, ci)] = []
                continue
            if current is not None:
                cbi, chf, hrow = current
                for ci, cell in enumerate(row):
                    if (cbi, ci) not in col_lines:
                        for oi, line in enumerate(cell_lines(cell)):
                            self._add(None, line, f"t{ti}.b{cbi}.c{ci}.r{ri}.o{oi}")
                        continue
                    for oi, line in enumerate(merge_wrapped(cell_lines(cell), chf.get(ci))):
                        self._add(chf.get(ci), line, f"t{ti}.b{cbi}.c{ci}.r{ri}.o{oi}")
                        col_lines[(cbi, ci)].append(line)
                continue
            # no header seen yet: a row whose first cells name a field is a row-labelled block
            label_ci = next((ci for ci, c in enumerate(row[:2]) if match_field(c, self.fields)), None)
            if label_ci is not None:
                f = match_field(row[label_ci], self.fields)
                lines = []
                for cj in range(label_ci + 1, len(row)):
                    for oi, line in enumerate(merge_wrapped(cell_lines(row[cj]), f)):
                        self._add(f, line, f"t{ti}.r{ri}.c{cj}.o{oi}")
                        lines.append(line)
                blocks.append(("ROW", clean_cell(row[label_ci]) + f" [t{ti}.r{ri}]", lines))
                if f.kind in (SINGLE, MULTI):
                    choice_hits += 1
            else:
                lines = []
                for cj, cell in enumerate(row):
                    for oi, line in enumerate(cell_lines(cell)):
                        self._add(None, line, f"t{ti}.r{ri}.c{cj}.o{oi}")
                        lines.append(line)
                if lines:
                    blocks.append(("OPTIONS", f"[t{ti}.r{ri}]", lines))

        col_blocks = [
            ("COLUMN", f"{title} [t{ti}.b{b}.c{c}]", col_lines[(b, c)])
            for b, c, title, f in order
            if col_lines.get((b, c)) is not None
        ]
        choice_hits += sum(1 for _, _, _, f in order if f is not None and f.kind in (SINGLE, MULTI))
        new_loose = self._loose[loose_before:]
        boxed = sum(1 for m in new_loose if m.box != "none")
        heavy = choice_hits > 0 or (len(new_loose) >= 3 and boxed / max(1, len(new_loose)) >= 0.3)
        if not heavy:
            return ["| " + " | ".join(r) + " |" for r in rows]
        out: List[str] = []
        for kind, title, lines in col_blocks + blocks:
            out.append(f"{kind} {title}:")
            out.extend(f"  {i}. {ln}" for i, ln in enumerate(lines, start=1))
        return out

    # -- layouts that are not tables -------------------------------------
    #
    # A report may print a field as "Sex: ☑ Male  ☐ Female" (inline), as a label line followed by
    # one option per line (list), as "Father: <name>" (label: value) or inside a sentence.
    # Each of these feeds the same per-field marks / text as a table column does.

    def _note_body(self, rows: List[List[str]]) -> None:
        """Remember a table's cell text as real content; a first row of column titles is left out."""
        data = rows
        if rows:
            filled = [c for c in rows[0] if clean_cell(c) and not is_blank(clean_cell(c))]
            titles = sum(1 for c in filled if match_field(c, self.fields) is not None or self._is_column_title(c))
            if len(filled) >= 2 and titles * 10 >= len(filled) * 6:
                data = rows[1:]
        for r in data:
            self._body.extend(clean_cell(c) for c in r if clean_cell(c))

    def _is_column_title(self, cell: str) -> bool:
        """A cell naming a column of one of the section's data tables (e.g. 'Member name', 'Sec. of law')."""
        t = _norm(re.sub(r"\(.*?\)", "", clean_cell(cell)))
        if len(t) < 3:
            return bool(re.fullmatch(r"s?l?\.?no\.?", t))
        for table in self.spec.tables:
            for col in table.columns:
                lab = _norm(col.label)
                if t == lab or (len(t) >= 4 and difflib.SequenceMatcher(None, t, lab).ratio() >= 0.8):
                    return True
        return bool(re.fullmatch(r"s?l?\.?no\.?", t))

    @staticmethod
    def _scan_options(f: FieldSpec, text: str, at: str) -> List[Mark]:
        """Each canonical option of ``f`` found in ``text`` with the marks written right around it.

        A box glyph belongs to exactly one option: the option that follows it ("☐ Lean  ☑ Stout"),
        or, when no option follows it directly, the one it trails ("Normal ☑").
        """
        spans = []
        for o in f.options:
            m = re.search(r"(?<![A-Za-z0-9])" + r"\s+".join(map(re.escape, o.split())) + r"(?![A-Za-z0-9])", text, re.I)
            if m:
                spans.append((m.start(), m.end(), o))
        spans.sort()
        glyphs: Dict[str, List[str]] = {}
        for gi, ch in enumerate(text):
            if ch not in _TICK_CHARS and ch not in _BOX_CHARS:
                continue
            nxt = next((s for s in spans if s[0] >= gi and re.fullmatch(r"[\s*_]*", text[gi + 1: s[0]])), None)
            prv = next((s for s in reversed(spans) if s[1] <= gi and re.fullmatch(r"[\s*_]{0,2}", text[s[1]: gi])), None)
            owner = nxt or prv
            if owner:
                glyphs.setdefault(owner[2], []).append(ch)
        found: List[Mark] = []
        for k, (start, end, o) in enumerate(spans):
            stop = spans[k + 1][0] if k + 1 < len(spans) else len(text)
            mine = glyphs.get(o, [])
            box = "ticked" if any(c in _TICK_CHARS for c in mine) else ("empty" if mine else "none")
            in_bold = text[:start].count("**") % 2 == 1
            yes = bool(re.match(r"^\W{0,3}(?:yes|yeas|yess)\b", text[end:stop], re.I)) and o.lower() not in ("yes", "no")
            found.append(Mark(o, text.strip(), box, in_bold, yes, at))
        return found

    def _label_line(self, text: str) -> Tuple[Optional[FieldSpec], str]:
        """(field, rest of line) when the line starts with a field's label, else (None, "").

        ``rest`` keeps the line's own markers (``**bold**``, box glyphs): they are the evidence.
        """
        body = re.sub(r"^[\s>*•\-]+", "", text).strip()
        head, sep, rest = body.partition(":")
        if sep and len(head) <= 60:
            f = match_field(head, self.fields)
            if f is not None:
                return f, rest.strip()
        flat = _norm(body.replace("**", ""))
        for g in self.fields:  # a long label followed by the answer without a colon
            label = _norm(g.label)
            if len(label) >= 12 and flat.startswith(label) and len(flat) > len(label):
                consumed, k = "", 0
                while k < len(body) and len(consumed) < len(label):
                    consumed += _norm(body[k].replace("*", ""))
                    k += 1
                return g, body[k:].strip()
        if len(body) <= 40:  # a bare label line such as "Height (3)"
            f = match_field(body, self.fields)
            if f is not None:
                return f, ""
        return None, ""

    def _from_label_value(self, f: FieldSpec, rest: str, at: str) -> None:
        self._texts.setdefault(f.key, []).append(clean_cell(re.sub(f"[{_TICK_CHARS}{_BOX_CHARS}]", "", rest)))
        if f.kind in (SINGLE, MULTI):
            marks = self._scan_options(f, rest, at) or [parse_mark(rest, at)]
            self._inline.setdefault(f.key, []).extend(marks)

    def _from_list_line(self, f: FieldSpec, text: str, at: str) -> None:
        self._texts.setdefault(f.key, []).append(clean_cell(re.sub(f"[{_TICK_CHARS}{_BOX_CHARS}]", "", text)))
        if f.kind in (SINGLE, MULTI):
            marks = self._scan_options(f, text, at) or [parse_mark(text, at)]
            self._inline.setdefault(f.key, []).extend(marks)

    def _scan_sentence(self, li: int, line: str) -> None:
        """Options inside running text: "Does the family stay together **Yes** No"."""
        flat = _norm(line)
        for f in self.choice_fields:
            label = _norm(f.label)
            if len(label) >= 6 and label in flat:
                found = self._scan_options(f, line, f"l{li}")
                if found:
                    self._inline.setdefault(f.key, []).extend(found)

    def _build(self, chunk: str) -> str:
        out: List[str] = []
        table: List[str] = []
        ti = 0

        def flush() -> None:
            nonlocal ti
            if not table:
                return
            rows = [
                [c.strip() for c in ln.strip().strip("|").split("|")]
                for ln in table
                if not set(ln.replace("|", "").strip()) <= set("-: ")
            ]
            self._note_body(rows)
            out.extend(self._table(ti, rows) if rows else [])
            ti += 1
            table.clear()

        current: Optional[FieldSpec] = None  # field whose option / value lines are being read
        for li, line in enumerate(chunk.splitlines()):
            if line.lstrip().startswith("|"):
                table.append(line)
                current = None
                continue
            flush()
            out.append(line)
            text = line.strip()
            if not text:
                current = None
                continue
            if li > 0:  # line 0 is the section heading, not content
                self._body.append(clean_cell(text))
            # line 0 is the section's own heading ("7.PERIOD OF OFFENCE:"), never a label of one of its fields
            f, rest = self._label_line(text) if li > 0 else (None, "")
            if f is not None and rest and not re.search(r"\w", rest):
                rest = ""  # only markup ("**") after the colon: no value
            if f is not None:
                current = f
                if rest:
                    self._from_label_value(f, rest, f"l{li}")
            elif current is not None:
                self._from_list_line(current, text, f"l{li}")
            else:
                self._scan_sentence(li, line)
        flush()
        return "\n".join(out)
