"""Local PaddleOCR-VL escalation for hard unresolved pages (Gate 4).

Only used when enabled, a local GPU is available (unless force-CPU is set),
and prior structure/OCR left critical gaps. Cloud VLMs are never called.
"""

from __future__ import annotations

import importlib.util
import io
import threading
from dataclasses import dataclass
from typing import Optional

from app.config import get_settings
from app.logging_utils import get_logger, safe_exc_message

logger = get_logger(__name__)

_lock = threading.Lock()
_engine = None
_init_error: Optional[str] = None


@dataclass
class VlResult:
    available: bool
    text: Optional[str] = None
    engine: str = "none"
    error: Optional[str] = None
    used_gpu: bool = False


def _cuda_available() -> bool:
    try:
        import paddle

        return bool(paddle.device.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0)
    except Exception:
        return False


def vl_installed() -> bool:
    # PaddleOCR VL may ship as paddleocr submodules or separate package names.
    return (
        importlib.util.find_spec("paddleocr") is not None
        or importlib.util.find_spec("paddlex") is not None
    )


class VlService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def gpu_ok(self) -> bool:
        if self.settings.vl_require_gpu:
            return _cuda_available()
        return True

    def is_available(self) -> bool:
        return bool(
            self.settings.vl_enabled
            and vl_installed()
            and self.gpu_ok()
            and _init_error is None
        )

    def status(self) -> dict:
        return {
            "enabled": self.settings.vl_enabled,
            "installed": vl_installed(),
            "gpu": _cuda_available(),
            "require_gpu": self.settings.vl_require_gpu,
            "available": self.is_available(),
            "error": _init_error,
        }

    def _get_engine(self):
        global _engine, _init_error
        if not self.settings.vl_enabled:
            return None
        if self.settings.vl_require_gpu and not _cuda_available():
            return None
        if _init_error is not None:
            return None
        with _lock:
            if _engine is not None:
                return _engine
            try:
                # Prefer dedicated VL pipeline when present (PaddleOCR 3 / PaddleX).
                try:
                    from paddleocr import PaddleOCRVL  # type: ignore

                    _engine = PaddleOCRVL()
                    logger.info("PaddleOCRVL initialized")
                    return _engine
                except Exception:
                    pass
                try:
                    from paddlex import create_pipeline  # type: ignore

                    _engine = create_pipeline(pipeline=self.settings.vl_pipeline or "PaddleOCR-VL")
                    logger.info("PaddleX VL pipeline initialized")
                    return _engine
                except Exception as exc:
                    _init_error = f"VL init failed: {safe_exc_message(exc)}"
                    logger.warning(_init_error)
                    return None
            except Exception as exc:
                _init_error = safe_exc_message(exc)
                return None

    def analyze_png(self, png_bytes: bytes) -> VlResult:
        if not self.settings.vl_enabled:
            return VlResult(available=False, error="VL_ENABLED=false")
        if self.settings.vl_require_gpu and not _cuda_available():
            return VlResult(available=False, error="VL requires GPU (VL_REQUIRE_GPU=true) and none is available")
        engine = self._get_engine()
        if engine is None:
            return VlResult(available=False, error=_init_error or "PaddleOCR-VL not installed")
        try:
            from PIL import Image
            import numpy as np

            img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
            arr = np.array(img)
            with _lock:
                if hasattr(engine, "predict"):
                    raw = engine.predict(arr)
                elif callable(engine):
                    raw = engine(arr)
                else:
                    raw = None
            text = _flatten_vl_text(raw)
            return VlResult(
                available=True,
                text=text,
                engine="paddleocr-vl",
                used_gpu=_cuda_available(),
            )
        except Exception as exc:
            return VlResult(available=True, error=safe_exc_message(exc), engine="paddleocr-vl")


def _flatten_vl_text(raw) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    parts = []
    if isinstance(raw, dict):
        for key in ("text", "markdown", "result", "content"):
            if key in raw and raw[key]:
                parts.append(str(raw[key]))
        return "\n".join(parts).strip() or None
    if isinstance(raw, (list, tuple)):
        for item in raw:
            t = _flatten_vl_text(item)
            if t:
                parts.append(t)
        return "\n".join(parts).strip() or None
    if hasattr(raw, "text"):
        return str(getattr(raw, "text") or "").strip() or None
    return str(raw).strip() or None
