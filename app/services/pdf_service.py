"""PDF text/table extraction via PyMuPDF (local)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import fitz  # PyMuPDF

from app.config import get_settings
from app.logging_utils import get_logger, safe_exc_message

logger = get_logger(__name__)


@dataclass
class PageContent:
    page_number: int  # 1-based
    text: str
    width: float
    height: float
    rotation: int
    char_count: int
    needs_ocr: bool = False
    tables: List[List[List[str]]] = field(default_factory=list)
    blocks: List[dict] = field(default_factory=list)


@dataclass
class PdfExtractionResult:
    path: Path
    page_count: int
    pages: List[PageContent]
    is_scanned: bool
    warnings: List[str] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        parts = []
        for p in self.pages:
            parts.append(f"--- PAGE {p.page_number} ---\n{p.text}")
        return "\n\n".join(parts)


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
            for i, page in enumerate(doc):
                rotation = int(page.rotation or 0)
                if rotation:
                    page.set_rotation(0)
                text = page.get_text("text") or ""
                blocks_raw = page.get_text("dict") or {}
                blocks = []
                for b in blocks_raw.get("blocks", []):
                    if b.get("type") != 0:
                        continue
                    lines = []
                    for line in b.get("lines", []):
                        span_text = "".join(s.get("text", "") for s in line.get("spans", []))
                        if span_text.strip():
                            lines.append(span_text.strip())
                    if lines:
                        blocks.append(
                            {
                                "bbox": b.get("bbox"),
                                "text": "\n".join(lines),
                            }
                        )
                tables = self._extract_tables(page)
                char_count = len(text.strip())
                needs_ocr = char_count < self.settings.ocr_min_text_chars
                pages.append(
                    PageContent(
                        page_number=i + 1,
                        text=text.strip(),
                        width=float(page.rect.width),
                        height=float(page.rect.height),
                        rotation=rotation,
                        char_count=char_count,
                        needs_ocr=needs_ocr,
                        tables=tables,
                        blocks=blocks,
                    )
                )
            page_count = len(pages)
            is_scanned = bool(pages) and sum(1 for p in pages if p.needs_ocr) >= max(
                1, int(0.5 * len(pages))
            )
            if is_scanned:
                warnings.append("Document appears scanned or image-based; OCR may be required")
            return PdfExtractionResult(
                path=path,
                page_count=page_count,
                pages=pages,
                is_scanned=is_scanned,
                warnings=warnings,
            )
        finally:
            doc.close()

    def render_page_image(self, path: Path, page_number: int) -> bytes:
        """Render a page to PNG bytes for local OCR only."""
        settings = self.settings
        doc = fitz.open(path)
        try:
            idx = page_number - 1
            if idx < 0 or idx >= len(doc):
                raise ValueError("Invalid page number")
            page = doc[idx]
            zoom = settings.ocr_dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            return pix.tobytes("png")
        finally:
            doc.close()

    @staticmethod
    def _extract_tables(page) -> List[List[List[str]]]:
        tables: List[List[List[str]]] = []
        try:
            finder = page.find_tables()
            for t in finder.tables:
                try:
                    data = t.extract()
                    cleaned = [
                        [((c or "").strip()) for c in row] for row in (data or []) if row
                    ]
                    if cleaned:
                        tables.append(cleaned)
                except Exception:
                    continue
        except Exception:
            # find_tables not available or failed — non-fatal
            pass
        return tables
