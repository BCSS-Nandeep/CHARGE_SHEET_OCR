"""Interrogation Report DOCX → same page/table model used by the PDF pipeline.

Word IR forms are table-heavy (Sl.No / Heads / value, header-over-value grids,
family / associate lists). This module reads those tables and section headings
into ``PdfExtractionResult`` so field detection stays identical to PDF.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Sequence, Set
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from app.config import get_settings
from app.logging_utils import get_logger, safe_exc_message
from app.services.layout_service import HeaderPair
from app.services.pdf_service import PageContent, PdfExtractionResult, PdfTable, TextLine
from app.services.text_normalize import soften_pdf_text

logger = get_logger(__name__)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SERIAL = re.compile(r"^[\(\[]?0?\d{1,2}[.)\]]?$")
_INDEXED_LABEL = re.compile(r"^(.+?)\s*\(\s*\d{1,2}\s*\)\s*$")
_GLUED_AADHAAR = re.compile(r"^(\d{4}\s*\d{4}\s*\d{4})\s*([A-Za-z].+)$")
# Column titles that must never be treated as field labels in a header grid
_COL_TITLE = re.compile(
    r"(?i)^(s\.?\s*no\.?|sl\.?\s*no\.?|heads?|identification\s+type|detail|relation|"
    r"address|phone\s*number|mobile\s*no\.?|name|crime\s*head|quantity|supplier|"
    r"from|to|dist/?|ps|unit|date of arrest|place of arrest)$"
)
# Physical-feature attribute names (with or without "(n)" index)
_PHYS_ATTR = re.compile(
    r"(?i)^(sex|built|height|eyes|hair|face|colou?r|teeth|nose|beard|mustache?s?|"
    r"ear|identification\s+marks?|deformities.*|language.*|burn\s+marks?|"
    r"leucodemm?a|leukoderma|mole|scar|tattoo)\b"
)


def _cell_text(tc: ET.Element) -> str:
    parts: List[str] = []
    for p in tc.findall(f".//{W}p"):
        runs: List[str] = []
        for t in p.iter(f"{W}t"):
            if t.text:
                runs.append(t.text)
            if t.tail:
                runs.append(t.tail)
        line = "".join(runs).strip()
        if line:
            parts.append(line)
    return "\n".join(parts).strip()


def _para_text(p: ET.Element) -> str:
    runs: List[str] = []
    for t in p.iter(f"{W}t"):
        if t.text:
            runs.append(t.text)
        if t.tail:
            runs.append(t.tail)
    return "".join(runs).strip()


def _table_rows(tbl: ET.Element) -> List[List[str]]:
    rows: List[List[str]] = []
    for tr in tbl.findall(f"{W}tr"):
        cells = [_cell_text(tc) for tc in tr.findall(f"{W}tc")]
        if any(c.strip() for c in cells):
            rows.append([soften_pdf_text(c) for c in cells])
    return rows


def _repair_glued_label(text: str) -> str:
    m = _GLUED_AADHAAR.match(text.strip())
    if m:
        return m.group(2).strip()
    return text


def _is_column_header(cells: Sequence[str]) -> bool:
    joined = " ".join(c.lower() for c in cells)
    markers = (
        "sl", "s no", "s.no", "heads", "identification type", "detail",
        "relation", "crime head", "type of drug", "consumer name",
        "a/c holder", "phone no", "imei",
    )
    hits = sum(1 for m in markers if m in joined)
    return hits >= 2


def _is_physical_feature_grid(labels: Sequence[str], values: Sequence[str]) -> bool:
    """Only Sex(1)/Built(2)/… style grids become header_table pairs."""
    labs = [c.strip() for c in labels if c and c.strip()]
    vals = [c.strip() for c in values if c and c.strip()]
    if len(labs) < 2 or len(vals) < 1:
        return False
    if any(_SERIAL.match(c) for c in labs):
        return False
    if any(_COL_TITLE.match(c) for c in labs):
        return False
    if _is_column_header(labels):
        return False
    # Values must not themselves look like a second header row
    if sum(1 for c in vals if _COL_TITLE.match(c) or _PHYS_ATTR.match(c) or _INDEXED_LABEL.match(c)) >= 2:
        return False
    indexed = sum(1 for c in labs if _INDEXED_LABEL.match(c))
    phys = sum(1 for c in labs if _PHYS_ATTR.match(_INDEXED_LABEL.sub(r"\1", c).strip()))
    return indexed >= 2 or phys >= 3


def _clean_label(lab: str) -> str:
    m = _INDEXED_LABEL.match(lab.strip())
    return m.group(1).strip() if m else lab.strip()


class DocxService:
    """Build a one-page native extraction result from an IR .docx."""

    def __init__(self) -> None:
        self.settings = get_settings()

    def extract(self, path: Path) -> PdfExtractionResult:
        warnings: List[str] = []
        try:
            with ZipFile(path) as zf:
                if "word/document.xml" not in zf.namelist():
                    raise ValueError("File is not a valid Word document (missing word/document.xml)")
                root = ET.fromstring(zf.read("word/document.xml"))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Unable to open DOCX: {safe_exc_message(exc)}") from exc

        body = root.find(f"{W}body")
        if body is None:
            raise ValueError("DOCX has no document body")

        tables: List[PdfTable] = []
        lines: List[TextLine] = []
        header_pairs: List[HeaderPair] = []
        text_chunks: List[str] = []
        y = 0.0
        order = 0

        for child in list(body):
            if child.tag == f"{W}p":
                text = soften_pdf_text(_para_text(child))
                if not text:
                    continue
                y += 14.0
                lines.append(TextLine(text=text, bbox=(40.0, y, 560.0, y + 12.0), in_table=False))
                text_chunks.append(text)
            elif child.tag == f"{W}tbl":
                rows = _table_rows(child)
                if not rows:
                    continue
                y += 20.0
                table_base_y = y
                consumed, order = self._emit_phys_grids(rows, header_pairs, table_base_y, order)
                data_rows = [r for i, r in enumerate(rows) if i not in consumed]
                if data_rows and _is_column_header(data_rows[0]):
                    data_rows = data_rows[1:]
                cleaned: List[List[str]] = []
                for row in data_rows:
                    fixed = [_repair_glued_label(c) for c in row]
                    while fixed and not fixed[-1].strip():
                        fixed.pop()
                    if any(c.strip() for c in fixed):
                        cleaned.append(fixed)
                # Advance Y past every row we handled (header grids + remaining form rows)
                rows_span = max(len(rows), 1)
                y = table_base_y + rows_span * 18.0
                if cleaned:
                    # Keep form-row tops inside the same vertical band as this table
                    tops = [table_base_y + i * 18.0 for i in range(len(cleaned))]
                    tables.append(
                        PdfTable(
                            bbox=(40.0, table_base_y, 560.0, y),
                            rows=cleaned,
                            row_tops=tops,
                        )
                    )
                    for row in cleaned:
                        text_chunks.append(" | ".join(c for c in row if c.strip()))

        # Stamp each header pair with a y based on emission order already in `order`
        # Re-y header pairs: they were appended with page-order via `order` index.
        # Extraction uses hp.order for y = base + order*0.01 — we need absolute y.
        # Store absolute y on HeaderPair via order field scaled: keep order as int
        # and pass y through a side channel.
        full_text = "\n".join(text_chunks)
        page = PageContent(
            page_number=1,
            text=full_text,
            width=595.0,
            height=max(842.0, y + 40.0),
            rotation=0,
            char_count=len(full_text),
            needs_ocr=False,
            route="native",
            route_reasons=["docx_native_tables"],
            native_text=full_text,
            tables=tables,
            lines=lines,
            image_count=0,
            image_coverage=0.0,
        )
        page._docx_header_pairs = header_pairs  # type: ignore[attr-defined]
        page._docx_header_ys = getattr(self, "_last_header_ys", {})  # type: ignore[attr-defined]

        if len(full_text.strip()) < 40:
            warnings.append("DOCX produced very little text; check the file content")

        return PdfExtractionResult(
            path=path,
            page_count=1,
            pages=[page],
            is_scanned=False,
            warnings=warnings,
        )

    def _emit_phys_grids(
        self,
        rows: List[List[str]],
        out: List[HeaderPair],
        base_y: float,
        order: int,
    ) -> tuple:
        """Emit only physical-feature label/value grids. Returns (consumed_indexes, order)."""
        if not hasattr(self, "_last_header_ys"):
            self._last_header_ys = {}
        consumed: Set[int] = set()
        i = 0
        while i < len(rows) - 1:
            labels, values = rows[i], rows[i + 1]
            if _is_physical_feature_grid(labels, values):
                n = min(len(labels), len(values))
                for j in range(n):
                    lab = labels[j].strip()
                    val = values[j].strip()
                    if not lab:
                        continue
                    hp = HeaderPair(label=_clean_label(lab), value=val, page=1, order=order)
                    out.append(hp)
                    self._last_header_ys[order] = base_y + (i * 18.0) + (j * 0.01)
                    order += 1
                consumed.add(i)
                consumed.add(i + 1)
                i += 2
                continue
            i += 1
        return consumed, order
