"""Hybrid page router — Gates 0/1 from the chargesheet OCR stack plan.

Deterministic signals from PyMuPDF decide native vs OCR vs both. No model
confidence alone routes a page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Set

# CCTNS / charge-sheet label hints used for form-likeness (Gate 0 / Gate 1)
_LABEL_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"\bdistrict\b",
        r"\bpolice\s*station\b|\b\bps\b",
        r"\bf\.?i\.?r\.?\b|\bfir\b|\bcr\.?\s*no",
        r"\bcharge\s*sheet\b|\bfinal\s*report\b",
        r"\bacts?\b|\bsections?\b",
        r"\baccused\b|\bA\s*-?\s*\d+\b",
        r"\bwitness",
        r"\binvestigat",
        r"\bcourt\b",
        r"\bBNSS\b|\bCr\.?\s*P\.?\s*C",
    )
]

_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_TELUGU = re.compile(r"[\u0C00-\u0C7F]")
_LATIN = re.compile(r"[A-Za-z]")


@dataclass
class PageSignals:
    page_index: int  # 1-based
    width: float
    height: float
    rotation: int = 0
    char_count: int = 0
    word_count: int = 0
    alpha_ratio: float = 0.0
    space_ratio: float = 0.0
    avg_word_len: float = 0.0
    image_count: int = 0
    image_coverage: float = 0.0
    table_count: int = 0
    table_rows: int = 0
    table_cols: int = 0
    label_hits: int = 0
    script_flags: Set[str] = field(default_factory=set)
    empty_cell_ratio: float = 0.0


@dataclass
class RouteDecision:
    route: str  # native | ocr | both
    reasons: List[str] = field(default_factory=list)
    ocr_lang_hint: str = "en"


def measure_text_signals(text: str) -> dict:
    stripped = (text or "").strip()
    chars = len(stripped)
    if chars == 0:
        return {
            "char_count": 0,
            "word_count": 0,
            "alpha_ratio": 0.0,
            "space_ratio": 0.0,
            "avg_word_len": 0.0,
            "label_hits": 0,
            "script_flags": set(),
        }
    words = re.findall(r"\S+", stripped)
    letters = sum(1 for c in stripped if c.isalpha())
    spaces = sum(1 for c in stripped if c.isspace())
    scripts: Set[str] = set()
    if _LATIN.search(stripped):
        scripts.add("latin")
    if _DEVANAGARI.search(stripped):
        scripts.add("devanagari")
    if _TELUGU.search(stripped):
        scripts.add("telugu")
    return {
        "char_count": chars,
        "word_count": len(words),
        "alpha_ratio": letters / chars,
        "space_ratio": spaces / chars,
        "avg_word_len": (sum(len(w) for w in words) / len(words)) if words else 0.0,
        "label_hits": sum(1 for p in _LABEL_PATTERNS if p.search(stripped)),
        "script_flags": scripts,
    }


def build_signals(
    *,
    page_number: int,
    width: float,
    height: float,
    rotation: int,
    text: str,
    image_count: int,
    image_coverage: float,
    table_count: int,
    table_rows: int = 0,
    table_cols: int = 0,
    empty_cell_ratio: float = 0.0,
) -> PageSignals:
    t = measure_text_signals(text)
    return PageSignals(
        page_index=page_number,
        width=width,
        height=height,
        rotation=rotation,
        char_count=t["char_count"],
        word_count=t["word_count"],
        alpha_ratio=t["alpha_ratio"],
        space_ratio=t["space_ratio"],
        avg_word_len=t["avg_word_len"],
        image_count=image_count,
        image_coverage=image_coverage,
        table_count=table_count,
        table_rows=table_rows,
        table_cols=table_cols,
        label_hits=t["label_hits"],
        script_flags=t["script_flags"],
        empty_cell_ratio=empty_cell_ratio,
    )


def decide_route(signals: PageSignals, *, min_native_chars: int = 80) -> RouteDecision:
    """Gate 1: native vs OCR vs both."""
    reasons: List[str] = []
    lang = "en"
    if "telugu" in signals.script_flags:
        lang = "te"
    elif "devanagari" in signals.script_flags:
        lang = "hi"

    force_ocr = False
    if signals.char_count == 0:
        force_ocr, reasons = True, reasons + ["char_count=0"]
    elif signals.image_coverage > 0.85 and signals.char_count < 500:
        force_ocr, reasons = True, reasons + ["image_coverage>0.85 with sparse text"]
    elif signals.char_count < min_native_chars and (
        signals.image_coverage > 0.5 or signals.label_hits == 0
    ):
        # Sparse + no form labels (or mostly an image) → OCR. Short native
        # form fragments that still carry labels (e.g. accused-only pages) stay native.
        force_ocr, reasons = True, reasons + [f"char_count<{min_native_chars} without usable labels"]

    if force_ocr:
        return RouteDecision(route="ocr", reasons=reasons, ocr_lang_hint=lang)

    suspicious = False
    if signals.space_ratio < 0.08 and signals.char_count >= min_native_chars:
        suspicious, reasons = True, reasons + ["space_ratio<0.08 (glued text)"]
    if signals.avg_word_len > 18 and signals.word_count >= 5:
        suspicious, reasons = True, reasons + ["avg_word_len>18"]
    if signals.alpha_ratio < 0.55 and signals.char_count >= min_native_chars:
        suspicious, reasons = True, reasons + ["alpha_ratio<0.55"]
    if (
        signals.char_count >= 500
        and signals.label_hits == 0
        and (signals.table_count > 0 or signals.image_coverage > 0.3)
    ):
        suspicious, reasons = True, reasons + ["form-like page without label hits"]

    if suspicious:
        return RouteDecision(route="both", reasons=reasons or ["suspicious native text"], ocr_lang_hint=lang)

    reasons.append("native text usable")
    return RouteDecision(route="native", reasons=reasons, ocr_lang_hint=lang)


def need_docling_assist(signals: PageSignals, route: str, *, enabled: bool) -> bool:
    """Gate 2: Docling only on digital/structured pages, never pure OCR scans."""
    if not enabled or route == "ocr":
        return False
    if signals.table_count >= 1 or signals.label_hits >= 5:
        if signals.empty_cell_ratio >= 0.35 or signals.table_count >= 1:
            return True
    return signals.label_hits >= 5 and signals.table_count >= 1


def ocr_accept(
    *,
    ocr_char_count: int,
    mean_confidence: float,
    low_conf_box_ratio: float,
    ocr_label_hits: int,
    min_chars: int = 80,
    min_confidence: float = 0.70,
) -> tuple[bool, List[str]]:
    """Gate 3: is Paddle/PP-Structure output good enough?"""
    reasons: List[str] = []
    if ocr_char_count < min_chars:
        reasons.append(f"ocr_char_count<{min_chars}")
    if mean_confidence < min_confidence and ocr_label_hits < 1:
        reasons.append(f"mean_confidence<{min_confidence} and no label hits")
    if low_conf_box_ratio > 0.30:
        reasons.append("low_conf_box_ratio>0.30")
    ok = ocr_char_count >= min_chars and (mean_confidence >= min_confidence or ocr_label_hits >= 1)
    if ok and low_conf_box_ratio > 0.30:
        # Acceptable but partial
        return True, reasons + ["partial: many low-confidence boxes"]
    return ok, reasons


def accused_numbering_gaps(serials: List[str]) -> List[int]:
    """Return missing A-numbers given extracted serial strings like A1, A-2, 3."""
    nums: List[int] = []
    for s in serials:
        m = re.search(r"(?i)(?:A\s*-?\s*)?(\d{1,3})", str(s or ""))
        if m:
            nums.append(int(m.group(1)))
    if not nums:
        return []
    lo, hi = min(nums), max(nums)
    present = set(nums)
    return [n for n in range(lo, hi + 1) if n not in present]


def count_label_hits(text: str) -> int:
    return sum(1 for p in _LABEL_PATTERNS if p.search(text or ""))
