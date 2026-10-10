"""Docling structured-text assist for digital CCTNS pages (Gate 2).

Runs only when enabled and the page router chose native/both with form/table
signals. Never used as the sole engine for full-page scans.
"""

from __future__ import annotations

import importlib.util
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from app.config import get_settings
from app.logging_utils import get_logger, safe_exc_message

logger = get_logger(__name__)

_lock = threading.Lock()
_converter = None
_init_error: Optional[str] = None


@dataclass
class DoclingResult:
    available: bool
    markdown: Optional[str] = None
    tables: List[List[List[str]]] = None  # type: ignore[assignment]
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if self.tables is None:
            self.tables = []


def docling_installed() -> bool:
    return importlib.util.find_spec("docling") is not None


class DoclingService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def is_available(self) -> bool:
        return bool(self.settings.docling_enabled and docling_installed() and _init_error is None)

    def status(self) -> dict:
        return {
            "enabled": self.settings.docling_enabled,
            "installed": docling_installed(),
            "available": self.is_available(),
            "error": _init_error,
        }

    def _get_converter(self):
        global _converter, _init_error
        if not self.settings.docling_enabled or not docling_installed():
            return None
        if _init_error is not None:
            return None
        with _lock:
            if _converter is not None:
                return _converter
            try:
                from docling.document_converter import DocumentConverter

                _converter = DocumentConverter()
                logger.info("Docling DocumentConverter initialized")
                return _converter
            except Exception as exc:
                _init_error = f"Docling init failed: {safe_exc_message(exc)}"
                logger.warning(_init_error)
                return None

    def analyze_pdf_page(self, pdf_path: Path, page_number: int) -> DoclingResult:
        """Convert a single-page PDF (or full PDF filtered later) via Docling."""
        if not self.settings.docling_enabled:
            return DoclingResult(available=False, error="DOCLING_ENABLED=false")
        conv = self._get_converter()
        if conv is None:
            return DoclingResult(available=False, error=_init_error or "Docling not installed")
        try:
            # Prefer a one-page temp PDF so Docling focuses on this page.
            single = _extract_single_page(pdf_path, page_number)
            with _lock:
                result = conv.convert(str(single))
            md = None
            tables: List[List[List[str]]] = []
            doc = getattr(result, "document", None)
            if doc is not None:
                if hasattr(doc, "export_to_markdown"):
                    md = doc.export_to_markdown()
                elif hasattr(doc, "export_to_text"):
                    md = doc.export_to_text()
            if single != pdf_path and single.exists():
                try:
                    single.unlink()
                except OSError:
                    pass
            return DoclingResult(available=True, markdown=md, tables=tables)
        except Exception as exc:
            return DoclingResult(available=True, error=safe_exc_message(exc))


def _extract_single_page(pdf_path: Path, page_number: int) -> Path:
    import fitz

    src = fitz.open(pdf_path)
    try:
        out = fitz.open()
        out.insert_pdf(src, from_page=page_number - 1, to_page=page_number - 1)
        tmp = Path(tempfile.mkstemp(suffix=".pdf")[1])
        out.save(tmp)
        out.close()
        return tmp
    finally:
        src.close()
