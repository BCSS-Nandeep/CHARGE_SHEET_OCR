"""Load ground-truth files and locate their source PDFs."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_GT_DIR = ROOT / "tests" / "fixtures" / "ground_truth"
PRIVATE_GT_DIR = ROOT / "tests" / "fixtures" / "private"


@dataclass
class GroundTruth:
    document_id: str
    path: Path
    data: dict
    private: bool

    @property
    def expected(self) -> dict:
        return self.data.get("expected", {})

    @property
    def requires_ocr(self) -> bool:
        return bool(self.data.get("requires_ocr"))

    @property
    def layout(self) -> str:
        return self.data.get("layout", "unknown")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _search_dirs() -> List[Path]:
    dirs = [ROOT / "uploads"]
    extra = os.environ.get("EVAL_DOCS_DIR")
    if extra:
        dirs.insert(0, Path(extra))
    return [d for d in dirs if d.is_dir()]


_hash_cache: dict = {}


def locate_source(gt: GroundTruth) -> Optional[Path]:
    src = gt.data.get("source", {})
    if src.get("fixture"):
        p = ROOT / src["fixture"]
        return p if p.exists() else None
    want = (src.get("sha256") or "").lower()
    if not want:
        return None
    for d in _search_dirs():
        for pdf in sorted(d.glob("*.pdf")):
            key = (str(pdf), pdf.stat().st_mtime)
            if key not in _hash_cache:
                _hash_cache[key] = _sha256(pdf)
            if _hash_cache[key] == want:
                return pdf
    return None


def load_ground_truth(include_private: bool = True) -> List[GroundTruth]:
    out: List[GroundTruth] = []
    dirs: Iterable = [(PUBLIC_GT_DIR, False)] + ([(PRIVATE_GT_DIR, True)] if include_private else [])
    for d, private in dirs:
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if "expected" not in data:
                continue
            out.append(GroundTruth(data.get("document_id", path.stem), path, data, private))
    return out
