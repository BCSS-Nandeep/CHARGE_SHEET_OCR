"""PP-Structure / layout table assist on top of PaddleOCR (optional).

When ``paddleocr`` exposes a structure/table pipeline, we use it to recover
grid cells on scanned form pages. Failures are soft — OCR line text remains.
"""

from __future__ import annotations

import importlib.util
import io
import re
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from app.config import get_settings
from app.logging_utils import get_logger, safe_exc_message

logger = get_logger(__name__)

_lock = threading.Lock()
_engine = None
_init_error: Optional[str] = None


@dataclass
class StructureTable:
    rows: List[List[str]] = field(default_factory=list)
    bbox: Optional[Tuple[float, float, float, float]] = None


@dataclass
class StructureResult:
    available: bool
    tables: List[StructureTable] = field(default_factory=list)
    markdown: Optional[str] = None
    engine: str = "none"
    error: Optional[str] = None


def structure_available() -> bool:
    if not get_settings().ppstructure_enabled:
        return False
    return importlib.util.find_spec("paddleocr") is not None


class StructureService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def is_available(self) -> bool:
        return structure_available() and _init_error is None

    def status(self) -> dict:
        return {
            "enabled": self.settings.ppstructure_enabled,
            "available": self.is_available(),
            "error": _init_error,
        }

    def _get_engine(self):
        global _engine, _init_error
        if not self.settings.ppstructure_enabled:
            return None
        if _init_error is not None:
            return None
        with _lock:
            if _engine is not None:
                return _engine
            try:
                # PaddleOCR 3.x layout/structure entry points vary by version.
                try:
                    from paddleocr import PPStructureV3  # type: ignore

                    _engine = PPStructureV3()
                    logger.info("PPStructureV3 initialized")
                    return _engine
                except Exception:
                    pass
                try:
                    from paddleocr import PPStructure  # type: ignore

                    _engine = PPStructure(show_log=False)
                    logger.info("PPStructure initialized")
                    return _engine
                except Exception as exc:
                    _init_error = f"PP-Structure init failed: {safe_exc_message(exc)}"
                    logger.warning(_init_error)
                    return None
            except Exception as exc:
                _init_error = safe_exc_message(exc)
                logger.warning("structure_unavailable err=%s", _init_error)
                return None

    def analyze_png(self, png_bytes: bytes) -> StructureResult:
        if not self.settings.ppstructure_enabled:
            return StructureResult(available=False, error="PPSTRUCTURE_ENABLED=false")
        engine = self._get_engine()
        if engine is None:
            return StructureResult(available=False, error=_init_error or "PP-Structure not available")
        try:
            from PIL import Image
            import numpy as np

            img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
            arr = np.array(img)
            with _lock:
                if hasattr(engine, "predict"):
                    raw = engine.predict(arr)
                else:
                    raw = engine(arr)
            tables, md = _parse_structure(raw)
            return StructureResult(
                available=True,
                tables=tables,
                markdown=md,
                engine="ppstructure",
            )
        except Exception as exc:
            return StructureResult(available=True, error=safe_exc_message(exc), engine="ppstructure")


def _parse_structure(raw) -> Tuple[List[StructureTable], Optional[str]]:
    tables: List[StructureTable] = []
    md_parts: List[str] = []
    if raw is None:
        return tables, None
    items = raw if isinstance(raw, list) else [raw]
    for item in items:
        if item is None:
            continue
        if isinstance(item, dict):
            _consume_dict(item, tables, md_parts)
            continue
        if hasattr(item, "keys"):
            try:
                d = {k: item[k] for k in item.keys()}
                _consume_dict(d, tables, md_parts)
            except Exception:
                continue
        # Classic PPStructure list of region dicts
        if isinstance(item, (list, tuple)):
            for region in item:
                if isinstance(region, dict):
                    _consume_dict(region, tables, md_parts)
    return tables, "\n\n".join(md_parts) if md_parts else None


def _consume_dict(d: dict, tables: List[StructureTable], md_parts: List[str]) -> None:
    typ = str(d.get("type") or d.get("label") or "").lower()
    res = d.get("res") or d.get("result") or d
    if "table" in typ or "html" in str(res).lower() or isinstance(res, dict) and "html" in res:
        html = ""
        if isinstance(res, dict):
            html = str(res.get("html") or res.get("markdown") or "")
            cells = res.get("cell_bbox") or res.get("cells")
            if isinstance(cells, list) and cells and isinstance(cells[0], list):
                rows = [[str(c or "").strip() for c in row] for row in cells]
                tables.append(StructureTable(rows=rows))
        elif isinstance(res, str):
            html = res
        if html:
            md_parts.append(html)
            rows = _html_table_rows(html)
            if rows:
                tables.append(StructureTable(rows=rows))
    text = d.get("text") or (res.get("text") if isinstance(res, dict) else None)
    if text and isinstance(text, str) and text.strip():
        md_parts.append(text.strip())


def _html_table_rows(html: str) -> List[List[str]]:
    rows: List[List[str]] = []
    for tr in re_findall_tr(html):
        cells = re_findall_td(tr)
        if cells:
            rows.append(cells)
    return rows


def re_findall_tr(html: str) -> List[str]:
    return re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.I | re.S)


def re_findall_td(tr: str) -> List[str]:
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.I | re.S)
    out = []
    for c in cells:
        text = re.sub(r"<[^>]+>", " ", c)
        text = re.sub(r"\s+", " ", text).strip()
        out.append(text)
    return out
