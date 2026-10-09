"""Local OCR — PaddleOCR preferred, optional local Tesseract fallback.

Never transmits images externally.
"""

from __future__ import annotations

import io
import shutil
import threading
from typing import List, Tuple

from app.config import get_settings
from app.logging_utils import get_logger, safe_exc_message

logger = get_logger(__name__)

_ocr_lock = threading.Lock()
_ocr_instance = None
_paddle_checked = False
_paddle_ok = False


def paddleocr_available() -> bool:
    global _paddle_ok, _paddle_checked
    if _paddle_checked:
        return _paddle_ok
    try:
        import paddleocr  # noqa: F401

        _paddle_ok = True
    except Exception:
        _paddle_ok = False
    _paddle_checked = True
    return _paddle_ok


def tesseract_available() -> bool:
    try:
        if shutil.which("tesseract"):
            return True
        import pytesseract  # noqa: F401

        return True
    except Exception:
        return False


class OcrService:
    """Offline OCR wrapper: prefers PaddleOCR, falls back to local Tesseract."""

    def __init__(self) -> None:
        self.settings = get_settings()

    def is_available(self) -> bool:
        if not self.settings.ocr_enabled:
            return False
        return paddleocr_available() or tesseract_available()

    def engine_name(self) -> str:
        if not self.settings.ocr_enabled:
            return "disabled"
        if paddleocr_available():
            return "paddleocr"
        if tesseract_available():
            return "tesseract"
        return "none"

    def _get_paddle(self):
        global _ocr_instance
        if not paddleocr_available():
            return None
        with _ocr_lock:
            if _ocr_instance is not None:
                return _ocr_instance
            try:
                from paddleocr import PaddleOCR

                # PaddleOCR 3.x dropped show_log / use_angle_cls; try modern
                # kwargs first, then fall back to the 2.x constructor.
                # Disable optional doc models to keep first-run downloads smaller.
                try:
                    _ocr_instance = PaddleOCR(
                        lang=self.settings.ocr_lang,
                        use_textline_orientation=False,
                        use_doc_orientation_classify=False,
                        use_doc_unwarping=False,
                    )
                except TypeError:
                    _ocr_instance = PaddleOCR(
                        use_angle_cls=True,
                        lang=self.settings.ocr_lang,
                        show_log=False,
                    )
                logger.info("PaddleOCR initialized locally")
                return _ocr_instance
            except Exception as exc:
                logger.warning("PaddleOCR init failed: %s", safe_exc_message(exc))
                return None

    @staticmethod
    def _parse_paddle_result(result) -> Tuple[List[str], List[float]]:
        """Normalize PaddleOCR 2.x / 3.x result shapes into (lines, confs)."""
        lines: List[str] = []
        confs: List[float] = []
        if not result:
            return lines, confs

        # 3.x: list of result objects / dicts with rec_texts + rec_scores
        for page in result:
            if page is None:
                continue
            if isinstance(page, dict):
                texts = page.get("rec_texts") or page.get("texts") or []
                scores = page.get("rec_scores") or page.get("scores") or []
                for i, txt in enumerate(texts):
                    if txt and str(txt).strip():
                        lines.append(str(txt).strip())
                        confs.append(float(scores[i]) if i < len(scores) else 0.5)
                continue
            # Some 3.x wrappers expose attributes instead of dict keys
            if hasattr(page, "rec_texts") or hasattr(page, "get"):
                try:
                    data = page if isinstance(page, dict) else dict(page)
                except Exception:
                    data = getattr(page, "res", None) or getattr(page, "__dict__", {})
                if isinstance(data, dict) and ("rec_texts" in data or "texts" in data):
                    texts = data.get("rec_texts") or data.get("texts") or []
                    scores = data.get("rec_scores") or data.get("scores") or []
                    for i, txt in enumerate(texts):
                        if txt and str(txt).strip():
                            lines.append(str(txt).strip())
                            confs.append(float(scores[i]) if i < len(scores) else 0.5)
                    continue
                texts = getattr(page, "rec_texts", None)
                scores = getattr(page, "rec_scores", None)
                if texts is not None:
                    scores = scores or []
                    for i, txt in enumerate(texts):
                        if txt and str(txt).strip():
                            lines.append(str(txt).strip())
                            confs.append(float(scores[i]) if i < len(scores) else 0.5)
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
                    txt, conf = str(text_info), 0.5
                if txt and str(txt).strip():
                    lines.append(str(txt).strip())
                    confs.append(conf)
        return lines, confs

    def recognize_png(self, png_bytes: bytes) -> Tuple[str, float]:
        """OCR a PNG image in-memory. Returns (text, mean_confidence)."""
        if not self.settings.ocr_enabled:
            return "", 0.0

        paddle = self._get_paddle()
        if paddle is not None:
            try:
                from PIL import Image
                import numpy as np

                img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
                arr = np.array(img)
                try:
                    result = paddle.ocr(arr)
                except TypeError:
                    # Older paddleocr accepted cls=
                    result = paddle.ocr(arr, cls=True)
                lines, confs = self._parse_paddle_result(result)
                mean_conf = sum(confs) / len(confs) if confs else 0.0
                return "\n".join(lines), mean_conf
            except Exception as exc:
                logger.warning("PaddleOCR failed: %s", safe_exc_message(exc))

        # Local Tesseract fallback (still offline)
        if tesseract_available():
            try:
                from PIL import Image
                import pytesseract

                img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
                text = pytesseract.image_to_string(img) or ""
                return text.strip(), 0.55 if text.strip() else 0.0
            except Exception as exc:
                logger.warning("Tesseract OCR failed: %s", safe_exc_message(exc))

        return "", 0.0
