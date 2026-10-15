"""Source provenance recorded in every combined export."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

from . import ENVELOPE_VERSION, UNIFIED_VERSION

_FILE = Path(__file__).with_name("provenance.json")


@lru_cache(maxsize=1)
def _load() -> Dict[str, Any]:
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ir_provenance() -> Dict[str, Any]:
    src = _load().get("ir_ocr", {})
    return {"implementation": "IR_OCR", "repository": _load().get("repository"), "branch": src.get("branch"),
            "commit": src.get("commit"), "mode": "in-process"}


def charge_sheet_provenance() -> Dict[str, Any]:
    src = _load().get("charge_sheet_ocr", {})
    return {"implementation": "CHARGE_SHEET_OCR", "repository": _load().get("repository"), "branch": src.get("branch"),
            "commit": src.get("commit"), "mode": "isolated worker process", "path": "vendor/charge_sheet_ocr"}


def unified_provenance() -> Dict[str, Any]:
    return {"unified_version": UNIFIED_VERSION, "envelope_version": ENVELOPE_VERSION}
