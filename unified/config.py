"""Settings of the unified layer (the two OCR services keep their own settings and ``.env`` files).

Values come from the process environment, then from the repository's ``.env`` (the same file the IR_OCR settings read;
unknown keys are ignored there, so nothing needs to change in the host's configuration).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parent.parent


def _dotenv() -> Dict[str, str]:
    try:
        from dotenv import dotenv_values

        return {k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None}
    except Exception:  # noqa: BLE001 - optional convenience
        return {}


def _truthy(value: Optional[str], default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _default_worker_python(worker_dir: Path) -> str:
    for candidate in (worker_dir / ".venv" / "Scripts" / "python.exe", worker_dir / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


@dataclass(frozen=True)
class UnifiedSettings:
    data_dir: Path
    worker_dir: Path
    worker_url: str
    worker_manage: bool
    worker_autostart: bool
    worker_python: str
    worker_startup_timeout: float
    worker_api_key: str
    classify_ocr: bool
    classify_min_confidence: float
    classify_margin: float
    probe_timeout: float
    max_probe_pages: int

    @property
    def registry_path(self) -> Path:
        return self.data_dir / "registry.db"

    @property
    def originals_dir(self) -> Path:
        return self.data_dir / "originals"

    @property
    def worker_data_dir(self) -> Path:
        return self.data_dir / "charge_sheet_worker"


def load_settings(overrides: Optional[Dict[str, str]] = None) -> UnifiedSettings:
    env = {**_dotenv(), **os.environ, **(overrides or {})}
    worker_dir = Path(env.get("CS_WORKER_DIR") or ROOT / "vendor" / "charge_sheet_ocr").resolve()
    return UnifiedSettings(
        data_dir=Path(env.get("UNIFIED_DATA_DIR") or ROOT / "data" / "unified").resolve(),
        worker_dir=worker_dir,
        worker_url=(env.get("CS_WORKER_URL") or "http://127.0.0.1:8101").rstrip("/"),
        worker_manage=_truthy(env.get("CS_WORKER_MANAGE"), True),
        worker_autostart=_truthy(env.get("CS_WORKER_AUTOSTART"), True),
        worker_python=env.get("CS_WORKER_PYTHON") or _default_worker_python(worker_dir),
        worker_startup_timeout=float(env.get("CS_WORKER_STARTUP_SECONDS") or 120),
        worker_api_key=env.get("CS_WORKER_API_KEY") or "",
        classify_ocr=_truthy(env.get("CLASSIFY_OCR"), True),
        classify_min_confidence=float(env.get("CLASSIFY_MIN_CONFIDENCE") or 0.55),
        classify_margin=float(env.get("CLASSIFY_MARGIN") or 0.25),
        probe_timeout=float(env.get("CLASSIFY_PROBE_TIMEOUT_SECONDS") or 240),
        max_probe_pages=int(env.get("CLASSIFY_PROBE_PAGES") or 2),
    )
