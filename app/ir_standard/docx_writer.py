"""Render the standard IR as a DOCX.

The same code draws the blank template (``data=None``) and a filled report, so
the output always has the standard 32-section layout whatever the uploaded
report looked like. In choice fields every option of the template is printed;
the selected ones show ``☒`` and bold, the others ``☐``.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Union

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from app.ir_standard.schema import MULTI, SECTION_BY_KEY, SECTIONS, SINGLE, FieldSpec, SectionSpec, TableSpec
from app.ir_standard.validator import blank_standard

CHECKED, UNCHECKED = "☒", "☐"  # ☒ ☐
FONT = "Calibri"
NEEDS_REVIEW = "NEEDS REVIEW"

# What each validation flag means for a reader of the printed report (the flag codes live in validator.py)
REASONS = {
    "unsupported_selection": "the option returned is not marked in the report (left empty)",
    "ungrounded_value_removed": "the value returned is not in the report (left empty)",
    "ungrounded_rows_removed": "table rows returned are not in the report (left out)",
    "choice_not_in_source": "the report's own text differs from the option returned (report text used)",
    "written_value_not_an_option": "the report writes this as text, not as a template option (kept as written)",
    "weak_evidence": "option marked only by bold or a written Yes, not a ticked box",
    "conflicting_evidence": "the report marks several options differently",
    "multiple_selected": "more than one option marked in a single-choice field",
    "missed_selection": "a marked option was not returned",
    "evidence_mismatch": "the evidence reported does not match the report",
    "evidence_unverified": "could not be checked against the report text",
    "unrecognized_option": "value matches no option of the template",
    "text_not_in_source": "text differs from what the report prints for this field",
    "missed_value": "the report has a value that was not returned",
    "missing_required": "required value not found",
    "implausible_value": "value looks implausible",
    "invalid_format": "value is not in the expected format",
    "section_not_found": "section not found in the uploaded report",
    "mapping_failed": "section could not be read",
}


@dataclass
class ReviewState:
    """Whether the report may be treated as final.

    ``final`` is True only when a reviewer explicitly verified the report (the workflow's verify step);
    extraction never sets it. ``resolved`` holds the ids of fields a reviewer corrected or confirmed.
    """

    final: bool = False
    verified_by: Optional[str] = None
    verified_at: Optional[str] = None
    resolved: Set[str] = field(default_factory=set)


class _Review:
    """Review markings for one rendered report: which fields to mark and with what text."""

    def __init__(self, standard: Dict[str, Any], state: ReviewState):
        self.state = state
        self.flags: Dict[str, List[dict]] = {}
        for f in standard.get("review_flags", []):
            self.flags.setdefault(f.get("path", ""), []).append(f)
        self.sections = {f["section"] for f in standard.get("review_flags", [])
                         if f.get("code") in ("section_not_found", "mapping_failed")}

    def open_flags(self, path: str) -> List[dict]:
        return [] if path in self.state.resolved else self.flags.get(path, [])

    def note(self, path: str) -> Optional[str]:
        """"NEEDS REVIEW: reason; reason" for a field a human has not resolved yet (draft reports only)."""
        if self.state.final:
            return None
        flags = self.open_flags(path)
        if not flags:
            return None
        reasons = list(dict.fromkeys(REASONS.get(f["code"], f["code"]) for f in flags))
        return f"{NEEDS_REVIEW}: " + "; ".join(reasons)

    def section_note(self, key: str) -> Optional[str]:
        return self.note(key) if key in self.sections else None

    def unresolved(self) -> List[str]:
        return [p for p in self.flags if p not in self.state.resolved]

    def total_flagged(self) -> int:
        return len(self.flags)


def _review_run(paragraph, text: str, size: float = 8) -> None:
    run = paragraph.add_run(text)
    run.bold = True
    run.font.size = Pt(size)
    run.font.name = FONT
    run.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
    run.font.highlight_color = WD_COLOR_INDEX.YELLOW


def _review_note(cell, note: Optional[str]) -> None:
    """Add the marker after the value; the value itself is never altered or removed."""
    if not note:
        return
    p = cell.add_paragraph()
    p.paragraph_format.space_after = Pt(0)
    _review_run(p, note)


# ---------------------------------------------------------------------------
# low-level helpers
# ---------------------------------------------------------------------------

def _set_cell_borders(table) -> None:
    table.style = "Table Grid"


def _shade(cell, fill: str = "E7E6E6") -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def _write(cell, text: Optional[str], bold: bool = False, align_center: bool = False, size: float = 9) -> None:
    """Replace a cell's content with ``text`` (newlines become separate paragraphs)."""
    lines = (text or "").split("\n")
    first = cell.paragraphs[0]
    for i, line in enumerate(lines):
        p = first if i == 0 else cell.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        if align_center:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(line)
        run.bold = bold
        run.font.size = Pt(size)
        run.font.name = FONT


def _options(cell, spec: FieldSpec, value: Optional[dict], inline: bool = False) -> None:
    """Print every option of ``spec`` with its box; selected ones are ☒ and bold."""
    chosen = set()
    other = None
    if value:
        sel = value.get("selected")
        chosen = set(sel) if isinstance(sel, list) else ({sel} if sel else set())
        other = value.get("other")
    first = cell.paragraphs[0]
    para = first
    for i, opt in enumerate(spec.options):
        if not inline or i == 0:
            para = first if i == 0 else cell.add_paragraph()
            para.paragraph_format.space_after = Pt(0)
        on = opt in chosen
        run = para.add_run(("   " if inline and i else "") + f"{CHECKED if on else UNCHECKED} {opt}")
        run.bold = on
        run.font.size = Pt(9)
        run.font.name = FONT
    if other:
        p = cell.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        r = p.add_run(f"Other: {other}")
        r.bold = True
        r.font.size = Pt(9)


def _text_value(value: Any) -> str:
    return "" if value is None else str(value)


def _plain_choice(value: Optional[dict]) -> str:
    """Selected option(s) as plain text, for choice cells inside data tables."""
    if not value:
        return ""
    sel = value.get("selected")
    text = ", ".join(sel) if isinstance(sel, list) else (sel or "")
    return text or (value.get("other") or "")


def _heading(doc, text: str, size: float = 11, center: bool = False) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.keep_with_next = True
    if center:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(text)
    r.bold = True
    r.font.size = Pt(size)
    r.font.name = FONT


def _para(doc, text: str, bold: bool = False, size: float = 9.5, center: bool = False) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    if center:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(text)
    r.bold = bold
    r.font.size = Pt(size)
    r.font.name = FONT


def _grid(doc, rows: int, cols: int):
    t = doc.add_table(rows=rows, cols=cols)
    _set_cell_borders(t)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    return t


# ---------------------------------------------------------------------------
# section layouts
# ---------------------------------------------------------------------------

def _kv_table(doc, fields: Iterable[FieldSpec], data: Dict[str, Any], inline: bool,
              rv: Optional[_Review] = None, section_key: str = "") -> None:
    fields = list(fields)
    if not fields:
        return
    t = _grid(doc, len(fields), 2)
    for r, f in enumerate(fields):
        label, cell = t.rows[r].cells
        _write(label, f.label, bold=True)
        _shade(label)
        if f.kind == "text":
            _write(cell, _text_value(data.get(f.key)))
        else:
            _options(cell, f, data.get(f.key), inline=inline)
        if rv is not None:
            _review_note(cell, rv.note(f"{section_key}.{f.key}"))
    for row in t.rows:
        row.cells[0].width, row.cells[1].width = Cm(5.5), Cm(12.5)


def _physical(doc, spec: SectionSpec, data: Dict[str, Any], rv: Optional[_Review] = None) -> None:
    fields = {f.key: f for f in spec.fields}

    def header(f: FieldSpec) -> str:
        return f"{f.label}\n({f.column_no})"

    def block(keys: List[str]) -> None:
        t = _grid(doc, 2, len(keys))
        for c, key in enumerate(keys):
            f = fields[key]
            _write(t.rows[0].cells[c], header(f), bold=True, align_center=True)
            _shade(t.rows[0].cells[c])
            if f.kind == "text":
                _write(t.rows[1].cells[c], _text_value(data.get(key)))
            else:
                _options(t.rows[1].cells[c], f, data.get(key))
            if rv is not None:
                _review_note(t.rows[1].cells[c], rv.note(f"{spec.key}.{key}"))
        doc.add_paragraph().paragraph_format.space_after = Pt(2)

    block(["sex", "built", "height", "eyes", "hair", "face", "color", "teeth"])
    block(["nose", "beard", "mustaches", "ear"])
    block(["identification_marks", "deformities", "language_dialect"])
    block(["burn_marks", "leucoderma", "mole", "scar", "tattoo"])


def _data_table(doc, spec: SectionSpec, table: TableSpec, rows: List[dict], caption: bool,
                rv: Optional[_Review] = None) -> None:
    if caption:
        _para(doc, table.title, bold=True)
    cols = table.columns
    shown: List[Optional[dict]] = list(rows)
    if not table.fixed_rows:
        shown += [None] * max(0, table.blank_rows - len(shown))
    t = _grid(doc, 1 + len(shown), 1 + len(cols))
    _write(t.rows[0].cells[0], "Sl. No.", bold=True, align_center=True)
    _shade(t.rows[0].cells[0])
    for c, col in enumerate(cols, start=1):
        _write(t.rows[0].cells[c], col.label, bold=True, align_center=True, size=8.5)
        _shade(t.rows[0].cells[c])
    for r, row in enumerate(shown, start=1):
        _write(t.rows[r].cells[0], str(r), align_center=True)
        for c, col in enumerate(cols, start=1):
            if row is None:
                continue
            v = row.get(col.key)
            _write(t.rows[r].cells[c], _text_value(v) if col.kind == "text" else _plain_choice(v), size=8.5)
            if rv is not None:
                _review_note(t.rows[r].cells[c], rv.note(f"{spec.key}.{table.key}.{r - 1}.{col.key}"))
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def _section(doc, spec: SectionSpec, data: Dict[str, Any], rv: Optional[_Review] = None) -> None:
    _heading(doc, spec.heading)
    if rv is not None and rv.section_note(spec.key):
        p = doc.add_paragraph()
        _review_run(p, rv.section_note(spec.key), size=9)  # an empty section here is "not read", not "empty"
    if spec.layout == "physical":
        _physical(doc, spec, data, rv)
        return
    if spec.layout == "tables":
        multi = len(spec.tables) > 1
        for t in spec.tables:
            _data_table(doc, spec, t, data.get(t.key) or [], caption=multi, rv=rv)
        _kv_table(doc, spec.fields, data, inline=True, rv=rv, section_key=spec.key)
        return
    _kv_table(doc, spec.fields, data, inline=(spec.layout == "choices"), rv=rv, section_key=spec.key)
    for t in spec.tables:
        _para(doc, t.title, bold=True)
        _data_table(doc, spec, t, data.get(t.key) or [], caption=False, rv=rv)


# ---------------------------------------------------------------------------
# document
# ---------------------------------------------------------------------------

def _narrative_block(doc, title: str, subtitle: str, lines: List[str]) -> None:
    _heading(doc, title, size=12, center=True)
    _para(doc, subtitle, bold=True, center=True)
    for line in lines:
        _para(doc, line, size=8.5, center=True)


def _status_banner(doc, rv: _Review) -> None:
    """Top-of-report box that says whether this is a draft needing review or a verified final report."""
    st = rv.state
    t = doc.add_table(rows=1, cols=1)
    _set_cell_borders(t)
    cell = t.rows[0].cells[0]
    open_n = len(rv.unresolved())
    if st.final:
        _shade(cell, "E2F0D9")
        lines = [("VERIFIED FINAL REPORT", True),
                 (f"Verified by {st.verified_by or 'a reviewer'}" + (f" on {st.verified_at}" if st.verified_at else "") + ".", False)]
        if rv.total_flagged():
            lines.append((f"{rv.total_flagged()} field(s) were flagged for review during extraction; the reviewer "
                          "confirmed the report with these in view.", False))
    else:
        _shade(cell, "FDE9E7")
        lines = [("DRAFT – NOT A VERIFIED FINAL REPORT", True),
                 ((f"{open_n} field(s) {NEEDS_REVIEW} (marked in red below and listed on the last page)."
                   if open_n else "No automatic flags were raised, but no reviewer has verified this report yet."), False),
                 ("Do not use as a final report until a reviewer verifies it in the workflow.", False)]
    first = True
    for text, bold in lines:
        p = cell.paragraphs[0] if first else cell.add_paragraph()
        first = False
        p.paragraph_format.space_after = Pt(0)
        r = p.add_run(text)
        r.bold = bold
        r.font.size = Pt(11 if bold else 9)
        r.font.name = FONT
        if not st.final:
            r.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def _review_summary(doc, standard: Dict[str, Any], rv: _Review) -> None:
    from app.ir_standard.paths import flatten

    labels = {f.id: f for f in flatten(standard)}
    open_paths = rv.unresolved()
    if not open_paths:
        return
    doc.add_page_break()
    _heading(doc, f"REVIEW SUMMARY – {len(open_paths)} item(s) {NEEDS_REVIEW}", size=12)
    _para(doc, "Values are shown exactly as extracted; none was changed or removed.", size=8.5)
    t = _grid(doc, 1 + len(open_paths), 3)
    for c, h in enumerate(("Field", "Value as returned", "Why it needs review")):
        _write(t.rows[0].cells[c], h, bold=True)
        _shade(t.rows[0].cells[c])
    for r, path in enumerate(open_paths, start=1):
        flat = labels.get(path)
        _write(t.rows[r].cells[0], flat.label if flat else path, size=8.5)
        value = (flat.value if flat else "") or "(empty)"
        _write(t.rows[r].cells[1], value if len(value) <= 120 else value[:119] + "…", size=8.5)
        why = "; ".join(dict.fromkeys(REASONS.get(f["code"], f["code"]) for f in rv.flags[path]))
        _write(t.rows[r].cells[2], why, size=8.5)


def build_document(standard: Optional[Dict[str, Any]] = None, review: Optional[ReviewState] = None):
    """python-docx Document of the standard IR (blank when ``standard`` is None).

    A filled report is a DRAFT unless ``review.final`` says a reviewer verified it: every field that
    still has an open validation flag is marked NEEDS REVIEW (the value stays as extracted), and a
    banner and page header say the report is not final.
    """
    std = standard or blank_standard()
    sections: Dict[str, Any] = std["sections"]
    rv = _Review(std, review or ReviewState()) if standard is not None else None
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(1.5)
    sec.top_margin = sec.bottom_margin = Cm(1.5)
    normal = doc.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(9.5)
    if rv is not None:
        head = sec.header.paragraphs[0]
        head.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if rv.state.final:
            text = "VERIFIED FINAL REPORT" + (f" – verified by {rv.state.verified_by}" if rv.state.verified_by else "")
            run = head.add_run(text)
        else:
            run = head.add_run("DRAFT – NOT VERIFIED – " + NEEDS_REVIEW)
            run.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
        run.bold = True
        run.font.size = Pt(9)
        _status_banner(doc, rv)

    def narrative_note(key: str) -> None:
        note = rv.note(f"s00_narrative.{key}") if rv is not None else None
        if note:
            _review_run(doc.add_paragraph(), note, size=9)

    n = sections.get("s00_narrative", {})
    _narrative_block(
        doc, "Annexure-I", "Interrogation Report",
        ["(Part-I Descriptive Particulars)", "(To be scanned and uploaded in CCTNS)"],
    )
    _para(doc, _text_value(n.get("part1_facts")), size=9.5)
    narrative_note("part1_facts")
    if n.get("part1_accused"):
        _para(doc, _text_value(n.get("part1_accused")), size=9.5)
    narrative_note("part1_accused")
    _para(doc, f"Date: {_text_value(n.get('report_date'))}", size=9.5)
    narrative_note("report_date")
    if n.get("officer"):
        _para(doc, _text_value(n.get("officer")), size=9.5)
    if n.get("officer_station"):
        _para(doc, _text_value(n.get("officer_station")), size=9.5)

    doc.add_page_break()
    _narrative_block(
        doc, "Annexure-II", "Interrogation report",
        ["(Part-II MO Criminal database inputs)", "(To be entered in CCTNS)"],
    )
    for spec in SECTIONS:
        if spec.number:
            _section(doc, spec, sections.get(spec.key, {}), rv)

    doc.add_page_break()
    _narrative_block(
        doc, "Annexure – III", "Interrogation report",
        ["(Part-III: MO Classification particulars)", "(To be entered in CCTNS)", "(MO=IXN)"],
    )
    _para(doc, _text_value(n.get("part3_facts")), size=9.5)
    narrative_note("part3_facts")
    if n.get("part3_registration"):
        _para(doc, _text_value(n.get("part3_registration")), size=9.5)
    narrative_note("part3_registration")
    if rv is not None and not rv.state.final:
        _review_summary(doc, std, rv)
    return doc


def render_docx(
    standard: Optional[Dict[str, Any]] = None,
    target: Union[str, Path, io.BytesIO, None] = None,
    review: Optional[ReviewState] = None,
) -> Optional[bytes]:
    """Write the document to ``target`` (path or stream); return the bytes when no target is given.

    Without ``review`` a filled report is rendered as a DRAFT; only ``ReviewState(final=True)`` (set from
    a reviewer's explicit verification) removes the draft marking.
    """
    doc = build_document(standard, review)
    if target is None:
        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()
    doc.save(target)
    return None
