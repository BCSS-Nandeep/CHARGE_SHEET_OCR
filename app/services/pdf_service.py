"""PDF text/table extraction via PyMuPDF (local)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import fitz  # PyMuPDF

from app.config import get_settings
from app.logging_utils import get_logger, safe_exc_message

logger = get_logger(__name__)

BBox = Tuple[float, float, float, float]


@dataclass
class TextLine:
    text: str
    bbox: BBox
    in_table: bool = False


@dataclass
class PdfTable:
    bbox: BBox
    rows: List[List[str]]
    row_tops: List[float] = field(default_factory=list)  # y0 of each row


@dataclass
class PageContent:
    page_number: int  # 1-based
    text: str
    width: float
    height: float
    rotation: int
    char_count: int
    needs_ocr: bool = False
    tables: List[PdfTable] = field(default_factory=list)
    lines: List[TextLine] = field(default_factory=list)
    image_count: int = 0


@dataclass
class PdfExtractionResult:
    path: Path
    page_count: int
    pages: List[PageContent]
    is_scanned: bool
    warnings: List[str] = field(default_factory=list)


class PdfService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def extract(self, path: Path) -> PdfExtractionResult:
        warnings: List[str] = []
        pages: List[PageContent] = []
        try:
            doc = fitz.open(path)
        except Exception as exc:
            raise ValueError(f"Unable to open PDF: {safe_exc_message(exc)}") from exc

        try:
            if doc.needs_pass:
                raise ValueError("PDF is password-protected; remove the password and re-upload")
            if len(doc) > self.settings.max_pages:
                raise ValueError(
                    f"PDF has {len(doc)} pages; the limit is {self.settings.max_pages} (MAX_PAGES)"
                )
            for i, page in enumerate(doc):
                rotation = int(page.rotation or 0)
                if rotation:
                    page.set_rotation(0)
                text = page.get_text("text") or ""
                tables = self._extract_tables(page)
                lines = self._extract_lines(page, tables)
                char_count = len(text.strip())
                pages.append(
                    PageContent(
                        page_number=i + 1,
                        text=text.strip(),
                        width=float(page.rect.width),
                        height=float(page.rect.height),
                        rotation=rotation,
                        char_count=char_count,
                        needs_ocr=char_count < self.settings.ocr_min_text_chars,
                        tables=tables,
                        lines=lines,
                        image_count=len(page.get_images(full=False)),
                    )
                )
            is_scanned = bool(pages) and sum(1 for p in pages if p.needs_ocr) >= max(
                1, int(0.5 * len(pages))
            )
            if is_scanned:
                warnings.append("Document appears scanned or image-based; OCR is required")
            return PdfExtractionResult(
                path=path,
                page_count=len(pages),
                pages=pages,
                is_scanned=is_scanned,
                warnings=warnings,
            )
        finally:
            doc.close()

    def render_page_image(self, path: Path, page_number: int) -> bytes:
        """Render a page to PNG bytes for local OCR only."""
        doc = fitz.open(path)
        try:
            idx = page_number - 1
            if idx < 0 or idx >= len(doc):
                raise ValueError("Invalid page number")
            zoom = self.settings.ocr_dpi / 72.0
            pix = doc[idx].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            return pix.tobytes("png")
        finally:
            doc.close()

    @staticmethod
    def _extract_lines(page, tables: List[PdfTable]) -> List[TextLine]:
        """Text lines with positions, flagged when they fall inside a detected table."""
        out: List[TextLine] = []
        try:
            raw = page.get_text("dict") or {}
        except Exception:
            return out
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                text = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
                if not text:
                    continue
                x0, y0, x1, y1 = line.get("bbox", (0, 0, 0, 0))
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                in_table = any(
                    t.bbox[0] - 1 <= cx <= t.bbox[2] + 1 and t.bbox[1] - 1 <= cy <= t.bbox[3] + 1
                    for t in tables
                )
                out.append(TextLine(text=text, bbox=(x0, y0, x1, y1), in_table=in_table))
        out.sort(key=lambda ln: (round(ln.bbox[1], 0), ln.bbox[0]))
        return out

    @staticmethod
    def _extract_tables(page) -> List[PdfTable]:
        tables: List[PdfTable] = []
        try:
            finder = page.find_tables()
        except Exception as exc:
            logger.debug("find_tables failed: %s", safe_exc_message(exc))
            return tables
        for t in finder.tables:
            try:
                data = t.extract() or []
                rows: List[List[str]] = []
                tops: List[float] = []
                row_objs = list(getattr(t, "rows", []) or [])
                for i, row in enumerate(data):
                    if not row:
                        continue
                    rows.append([(c or "").strip() for c in row])
                    if i < len(row_objs) and getattr(row_objs[i], "bbox", None):
                        tops.append(float(row_objs[i].bbox[1]))
                    else:
                        # Fallback: spread rows evenly over the table height
                        tops.append(t.bbox[1] + (t.bbox[3] - t.bbox[1]) * i / max(len(data), 1))
                if rows and _is_form_table(rows):
                    tables.append(PdfTable(bbox=tuple(t.bbox), rows=rows, row_tops=tops))
            except Exception:
                continue
        return tables


def _is_form_table(rows: List[List[str]]) -> bool:
    """Form grids number their rows; ruled-looking prose blocks do not.

    find_tables() also fires on justified narrative paragraphs; treating those
    as tables would hide narrative lines from the text stream.
    """
    firsts = [next((c for c in row if c), "") for row in rows]
    firsts = [f for f in firsts if f]
    if not firsts:
        return False
    serial = sum(1 for f in firsts if re.fullmatch(r"\d{1,2}[.)]?", f))
    return serial >= 1 and serial >= 0.3 * len(firsts)


def page_free_text(page: PageContent) -> Optional[str]:
    """Text of lines outside detected tables (None when the page has no tables)."""
    if not page.tables:
        return None
    return "\n".join(line.text for line in page.lines if not line.in_table)
