"""Local OCR — PaddleOCR preferred, optional local Tesseract fallback.

Never transmits images externally. Engines are only reported as available
when they can actually run on this host (a pip-installed ``pytesseract``
wrapper without the tesseract binary is *not* an OCR engine).
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from app.config import get_settings
from app.logging_utils import get_logger, safe_exc_message

logger = get_logger(__name__)

# PaddleX probes HuggingFace / ModelScope / AIStudio / BOS connectivity when it
# builds its model hoster list. Models must be prefetched; never probe.
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

_ocr_lock = threading.Lock()
_ocr_instance = None
_paddle_init_error: Optional[str] = None

_WINDOWS_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


@dataclass
class OcrLine:
    text: str
    confidence: float
    bbox: Tuple[float, float, float, float]  # x0, y0, x1, y1 in image pixels
    poly: Optional[List[Tuple[float, float]]] = None  # rotated quad (tl, tr, br, bl) when available


@dataclass
class OcrResult:
    engine: str
    lines: List[OcrLine] = field(default_factory=list)
    image_width: int = 0
    image_height: int = 0
    error: Optional[str] = None

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def mean_confidence(self) -> float:
        if not self.lines:
            return 0.0
        return sum(line.confidence for line in self.lines) / len(self.lines)


def paddleocr_available() -> bool:
    """Importable without importing (importing paddle takes seconds)."""
    return (
        importlib.util.find_spec("paddleocr") is not None
        and importlib.util.find_spec("paddle") is not None
    )


def tesseract_binary() -> Optional[str]:
    configured = (get_settings().tesseract_cmd or "").strip()
    if configured:
        return configured if Path(configured).is_file() else None
    found = shutil.which("tesseract")
    if found:
        return found
    if _WINDOWS_TESSERACT.is_file():
        return str(_WINDOWS_TESSERACT)
    return None


def tesseract_available() -> bool:
    return tesseract_binary() is not None and importlib.util.find_spec("pytesseract") is not None


class OcrService:
    """Offline OCR wrapper: prefers PaddleOCR, falls back to local Tesseract."""

    def __init__(self) -> None:
        self.settings = get_settings()

    def is_available(self) -> bool:
        if not self.settings.ocr_enabled:
            return False
        paddle_usable = paddleocr_available() and _paddle_init_error is None
        return paddle_usable or tesseract_available()

    def engine_name(self) -> str:
        if not self.settings.ocr_enabled:
            return "disabled"
        if paddleocr_available() and _paddle_init_error is None:
            return "paddleocr"
        if tesseract_available():
            return "tesseract"
        return "none"

    def status(self) -> dict:
        return {
            "enabled": self.settings.ocr_enabled,
            "engine": self.engine_name(),
            "paddleocr_installed": paddleocr_available(),
            "paddleocr_error": _paddle_init_error,
            "tesseract_binary": bool(tesseract_binary()),
        }

    def _get_paddle(self):
        global _ocr_instance, _paddle_init_error
        if not paddleocr_available() or _paddle_init_error is not None:
            return None
        with _ocr_lock:
            if _ocr_instance is not None:
                return _ocr_instance
            try:
                from paddleocr import PaddleOCR

                try:
                    # PaddleOCR 3.x
                    tuning = {}
                    if self.settings.ocr_det_limit_side_len > 0:
                        tuning = {
                            "text_det_limit_type": "max",
                            "text_det_limit_side_len": self.settings.ocr_det_limit_side_len,
                        }
                    if self.settings.ocr_det_box_thresh > 0:
                        tuning["text_det_box_thresh"] = self.settings.ocr_det_box_thresh
                    orient = bool(self.settings.ocr_use_orientation)
                    _ocr_instance = PaddleOCR(
                        lang=self.settings.ocr_lang,
                        use_textline_orientation=orient,
                        use_doc_orientation_classify=orient,
                        use_doc_unwarping=False,
                        enable_mkldnn=self.settings.ocr_enable_mkldnn,
                        cpu_threads=self.settings.ocr_cpu_threads,
                        **tuning,
                    )
                except TypeError:
                    # PaddleOCR 2.x constructor
                    _ocr_instance = PaddleOCR(
                        use_angle_cls=True,
                        lang=self.settings.ocr_lang,
                        show_log=False,
                        enable_mkldnn=self.settings.ocr_enable_mkldnn,
                    )
                logger.info("PaddleOCR initialized locally")
                return _ocr_instance
            except Exception as exc:
                _paddle_init_error = (
                    f"PaddleOCR init failed ({safe_exc_message(exc)}). Models must be "
                    "cached locally; downloads are blocked in offline mode."
                )
                logger.warning(_paddle_init_error)
                return None

    @staticmethod
    def _parse_paddle_result(result) -> List[OcrLine]:
        """Normalize PaddleOCR 2.x / 3.x result shapes into OcrLines."""
        lines: List[OcrLine] = []
        if not result:
            return lines
        for page in result:
            if page is None:
                continue
            data = None
            if isinstance(page, dict):
                data = page
            elif hasattr(page, "keys") and hasattr(page, "__getitem__"):
                try:
                    data = {k: page[k] for k in page.keys()}
                except Exception:
                    data = None
            if data is not None and ("rec_texts" in data or "texts" in data):
                texts = data.get("rec_texts") or data.get("texts") or []
                scores = data.get("rec_scores") if data.get("rec_scores") is not None else []
                polys = data.get("rec_polys")
                boxes = data.get("rec_boxes")
                if boxes is None:
                    boxes = polys
                for i, txt in enumerate(texts):
                    if not txt or not str(txt).strip():
                        continue
                    conf = float(scores[i]) if i < len(scores) else 0.0
                    bbox = _to_bbox(boxes[i]) if boxes is not None and i < len(boxes) else (0.0, 0.0, 0.0, 0.0)
                    poly = _to_poly(polys[i]) if polys is not None and i < len(polys) else None
                    lines.append(OcrLine(str(txt).strip(), conf, bbox, poly))
                continue
            # 2.x: [[[box], (text, conf)], ...]
            if not isinstance(page, (list, tuple)):
                continue
            for item in page:
                if not item or not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                text_info = item[1]
                if isinstance(text_info, (list, tuple)) and len(text_info) >= 2:
                    txt, conf = text_info[0], float(text_info[1])
                else:
                    txt, conf = str(text_info), 0.0
                if txt and str(txt).strip():
                    lines.append(OcrLine(str(txt).strip(), conf, _to_bbox(item[0]), _to_poly(item[0])))
        return lines

    def recognize_png(self, png_bytes: bytes) -> OcrResult:
        """OCR a PNG image in-memory. Errors are reported, never swallowed as 'no text'."""
        if not self.settings.ocr_enabled:
            return OcrResult(engine="disabled", error="OCR disabled (OCR_ENABLED=false)")

        from PIL import Image

        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        width, height = img.size
        errors: List[str] = []

        paddle = self._get_paddle()
        if paddle is not None:
            try:
                import numpy as np

                arr = np.array(img)
                with _ocr_lock:
                    if hasattr(paddle, "predict"):
                        result = paddle.predict(arr)
                    else:
                        result = paddle.ocr(arr, cls=True)
                return OcrResult(
                    engine="paddleocr",
                    lines=self._parse_paddle_result(result),
                    image_width=width,
                    image_height=height,
                )
            except Exception as exc:
                msg = f"PaddleOCR failed: {safe_exc_message(exc)}"
                logger.warning(msg)
                errors.append(msg)
        elif _paddle_init_error:
            errors.append(_paddle_init_error)

        binary = tesseract_binary()
        if binary and importlib.util.find_spec("pytesseract") is not None:
            try:
                return OcrResult(
                    engine="tesseract",
                    lines=_tesseract_lines(img, binary, self.settings.ocr_lang),
                    image_width=width,
                    image_height=height,
                )
            except Exception as exc:
                msg = f"Tesseract failed: {safe_exc_message(exc)}"
                logger.warning(msg)
                errors.append(msg)

        if not errors:
            errors.append("No local OCR engine installed (PaddleOCR or Tesseract)")
        return OcrResult(engine="none", image_width=width, image_height=height, error="; ".join(errors))


def _to_bbox(box) -> Tuple[float, float, float, float]:
    """Accept [x0,y0,x1,y1] or a 4-point polygon [[x,y], ...]."""
    try:
        items = box.tolist() if hasattr(box, "tolist") else list(box)
        flat: List[float] = []
        for item in items:
            if isinstance(item, (list, tuple)):
                flat.extend(float(v) for v in item)
            else:
                flat.append(float(item))
    except Exception:
        return (0.0, 0.0, 0.0, 0.0)
    if len(flat) == 4:
        x0, y0, x1, y1 = flat
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    xs, ys = flat[0::2], flat[1::2]
    if not xs or not ys:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _to_poly(points) -> Optional[List[Tuple[float, float]]]:
    """4-point polygon [[x,y]*4] → list of tuples; None for axis-aligned [x0,y0,x1,y1]."""
    try:
        items = points.tolist() if hasattr(points, "tolist") else list(points)
        if len(items) == 4 and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in items):
            return [(float(x), float(y)) for x, y in items]
    except Exception:
        pass
    return None


def _tesseract_lines(img, binary: str, lang: str) -> List[OcrLine]:
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = binary
    tess_lang = {"en": "eng", "hi": "hin"}.get(lang, lang)
    data = pytesseract.image_to_data(img, lang=tess_lang, output_type=pytesseract.Output.DICT)
    grouped: dict = {}
    for i, word in enumerate(data.get("text", [])):
        if not word or not word.strip():
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        conf = float(data["conf"][i]) / 100.0 if float(data["conf"][i]) >= 0 else 0.0
        grouped.setdefault(key, []).append((word.strip(), conf, (x, y, x + w, y + h)))
    lines: List[OcrLine] = []
    for words in grouped.values():
        text = " ".join(w for w, _, _ in words)
        conf = sum(c for _, c, _ in words) / len(words)
        x0 = min(b[0] for _, _, b in words)
        y0 = min(b[1] for _, _, b in words)
        x1 = max(b[2] for _, _, b in words)
        y1 = max(b[3] for _, _, b in words)
        lines.append(OcrLine(text, conf, (float(x0), float(y0), float(x1), float(y1))))
    lines.sort(key=lambda line: (line.bbox[1], line.bbox[0]))
    return lines
