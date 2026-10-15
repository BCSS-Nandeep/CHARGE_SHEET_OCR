"""Reusable label→canonical field mapping (template-independent)."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from functools import lru_cache
from typing import List, Optional, Tuple

from app.services.field_synonyms import FIELD_SYNONYMS, FORM_LABEL_HINTS, LIST_HEADING_PATTERNS
from app.services.text_normalize import compact

_WS = re.compile(r"\s+")
_GENERIC_DATE = re.compile(r"^(?:date|dated|dt|on)\.?$", re.I)

# Prefix for canonical names of recognised-but-unmapped form labels
HINT_PREFIX = "hint:"


def normalize_label(label: str) -> str:
    text = label.lower().strip()
    text = text.replace(" ", " ")
    text = re.sub(r"[^\w\s./&()]", " ", text)
    text = _WS.sub(" ", text).strip()
    return text


def _whole_word(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


@lru_cache(maxsize=1)
def _vocabulary() -> List[Tuple[str, str, str]]:
    """(canonical, normalized synonym, compact synonym) for fields and hints."""
    vocab: List[Tuple[str, str, str]] = []
    for canonical, synonyms in FIELD_SYNONYMS.items():
        for syn in synonyms:
            vocab.append((canonical, normalize_label(syn), compact(syn)))
    for hint, phrases in FORM_LABEL_HINTS.items():
        for phrase in phrases:
            vocab.append((HINT_PREFIX + hint, normalize_label(phrase), compact(phrase)))
    return vocab


def _score(norm: str, comp: str, syn_n: str, syn_c: str) -> float:
    if not syn_n:
        return 0.0
    if norm == syn_n:
        return 1.0
    if comp and comp == syn_c:
        return 0.97
    score = 0.0
    # Whole-word containment, weighted by how much of the label it explains;
    # a key phrase at the start of the label is the strongest signal.
    if len(syn_n) >= 3 and _whole_word(norm, syn_n):
        coverage = len(syn_n) / max(len(norm), 1)
        score = 0.78 + 0.17 * coverage + (0.05 if norm.startswith(syn_n) else 0.0)
    elif len(norm) >= 4 and len(syn_n) >= 4 and _whole_word(syn_n, norm):
        score = 0.88 * len(norm) / len(syn_n) + 0.05
    # Glued labels ("NameoftheI.Os.,Includingrankand") — compact containment
    if len(syn_c) >= 6 and syn_c in comp:
        coverage = len(syn_c) / max(len(comp), 1)
        score = max(score, 0.75 + 0.2 * coverage + (0.05 if comp.startswith(syn_c) else 0.0))
    # Fuzzy only when lengths are comparable (avoid 'occ' ≈ random)
    if abs(len(norm) - len(syn_n)) <= max(4, len(syn_n) // 2) and len(norm) >= 4:
        score = max(score, SequenceMatcher(None, norm, syn_n).ratio())
    return min(score, 0.99)


def best_canonical_match(
    label: str,
    threshold: float = 0.8,
    *,
    include_hints: bool = False,
) -> Tuple[Optional[str], float]:
    """Map an arbitrary label to a canonical field name using synonyms + fuzzy match.

    Short synonyms must match as whole words to avoid:
    - 'year' matching inside 'years'
    - 'occ' matching inside 'occurrence' / 'occupation'
    - 'sho' matching inside 'Bishop'

    With ``include_hints`` a recognised non-schema form label may win; its
    canonical is returned as ``hint:<name>`` so callers keep it unmapped.
    """
    from app.services.text_normalize import looks_like_person_label

    norm = normalize_label(label)
    comp = compact(label)
    if not norm:
        return None, 0.0

    # Person/narrative fragments are never case-field labels
    if looks_like_person_label(label):
        return None, 0.0

    best_name: Optional[str] = None
    best_score = 0.0
    for canonical, syn_n, syn_c in _vocabulary():
        if not include_hints and canonical.startswith(HINT_PREFIX):
            continue
        score = _score(norm, comp, syn_n, syn_c)
        if score > best_score:
            best_score, best_name = score, canonical

    if best_score >= threshold:
        return best_name, round(best_score, 3)
    return None, round(best_score, 3)


def is_generic_date_label(label: str) -> bool:
    """'Date', 'Dated', 'Dt.', 'On' — meaning depends on the neighbouring field."""
    return bool(_GENERIC_DATE.match((label or "").strip().strip(":").strip()))


def list_heading_kind(label: str) -> Optional[str]:
    """accused_charged | accused_not_charged | witnesses | None for a heading label."""
    comp = compact(label)
    if not comp or len(comp) > 90:
        return None
    for kind, pattern in LIST_HEADING_PATTERNS:
        if re.search(pattern, comp):
            return kind
    return None
