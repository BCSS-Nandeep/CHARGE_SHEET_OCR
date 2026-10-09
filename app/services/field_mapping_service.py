"""Reusable label→canonical field mapping (template-independent)."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from app.services.field_synonyms import FIELD_SYNONYMS

_LABEL_SPLIT = re.compile(
    r"[:：\-–—|]|(?<=\d)[\.\)]\s+",
)
_NUMBERED = re.compile(r"^\s*(\d{1,3})[\.\)\-]?\s*(.+)$")
_WS = re.compile(r"\s+")


def normalize_label(label: str) -> str:
    text = label.lower().strip()
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[^\w\s./&()]", " ", text)
    text = _WS.sub(" ", text).strip()
    return text


def _whole_word(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def best_canonical_match(label: str, threshold: float = 0.78) -> Tuple[Optional[str], float]:
    """Map an arbitrary label to a canonical field name using synonyms + fuzzy match.

    Short synonyms must match as whole words to avoid:
    - 'year' matching inside 'years'
    - 'occ' matching inside 'occurrence' / 'occupation'
    - 'sho' matching inside 'Ashok'
    """
    from app.services.text_normalize import looks_like_person_label

    norm = normalize_label(label)
    if not norm:
        return None, 0.0

    # Person/narrative fragments are never case-field labels
    if looks_like_person_label(label):
        return None, 0.0

    best_name: Optional[str] = None
    best_score = 0.0
    for canonical, synonyms in FIELD_SYNONYMS.items():
        for syn in synonyms:
            syn_n = normalize_label(syn)
            if not syn_n:
                continue
            if norm == syn_n:
                return canonical, 1.0

            # Whole-word containment only (and reject tiny needles)
            if len(syn_n) >= 3 and _whole_word(norm, syn_n):
                # Prefer longer synonym hits
                score = 0.9 + min(0.08, 0.01 * len(syn_n))
                if score > best_score:
                    best_score = score
                    best_name = canonical
            elif len(norm) >= 4 and len(syn_n) >= 4 and _whole_word(syn_n, norm):
                score = 0.88
                if score > best_score:
                    best_score = score
                    best_name = canonical

            # Fuzzy only when lengths are comparable (avoid 'occ' ≈ random)
            if abs(len(norm) - len(syn_n)) <= max(4, len(syn_n) // 2) and len(norm) >= 4:
                ratio = SequenceMatcher(None, norm, syn_n).ratio()
                if ratio > best_score:
                    best_score = ratio
                    best_name = canonical

    if best_score >= threshold:
        return best_name, best_score
    return None, best_score


def split_label_value(line: str) -> Tuple[Optional[str], Optional[str]]:
    """Attempt to split 'Label : Value' or '1. Label  Value' lines."""
    raw = line.strip()
    if not raw or len(raw) < 2:
        return None, None

    # Numbered form field: "1. District          XYZ"
    m = _NUMBERED.match(raw)
    if m:
        rest = m.group(2).strip()
        # split on 2+ spaces
        parts = re.split(r"\s{2,}", rest, maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            label = parts[0].strip(" :")
            value = parts[1].lstrip(" :").strip()
            return label, value or None
        if ":" in rest:
            a, b = rest.split(":", 1)
            return a.strip(), b.strip() or None

    if ":" in raw:
        a, b = raw.split(":", 1)
        if a.strip() and len(a) < 80:
            return a.strip(), b.strip() or None

    # Tab-separated
    if "\t" in raw:
        a, b = raw.split("\t", 1)
        return a.strip(), b.strip() or None

    # Space-aligned label/value without colon: "Distt.          Sample East"
    parts = re.split(r"\s{2,}", raw, maxsplit=1)
    if len(parts) == 2 and parts[1].strip() and len(parts[0]) < 80:
        return parts[0].strip(), parts[1].strip()

    return None, None


def extract_kv_candidates(text: str, page_number: int) -> List[dict]:
    """Extract label/value candidates from page text."""
    candidates: List[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        label, value = split_label_value(line)
        if not label:
            continue
        canonical, score = best_canonical_match(label)
        candidates.append(
            {
                "label": label,
                "value": value,
                "canonical": canonical,
                "mapping_confidence": score,
                "source_page": page_number,
                "source_text": line[:300],
            }
        )
    return candidates


def extract_acts_sections(text: str, page_number: int) -> List[dict]:
    """Find IPC/BNS/CrPC style section references."""
    pattern = re.compile(
        r"(?P<act>\b(?:IPC|BNS|Cr\.?\s*P\.?\s*C\.?|BNSS|Arms\s+Act|NDPS|IT\s+Act|POCSO)"
        r"[A-Za-z.\s]*)?\s*(?:[Uu]/[Ss]/|sections?|sec\.?)\s*"
        r"(?P<secs>[\dA-Za-z,\s\-–and&/]+)",
        re.IGNORECASE,
    )
    found: List[dict] = []
    for m in pattern.finditer(text):
        act = (m.group("act") or "").strip() or None
        secs = re.sub(r"\s+", " ", m.group("secs")).strip(" ,./")
        if not secs or len(secs) > 80:
            continue
        found.append(
            {
                "act": act,
                "section": secs,
                "source_page": page_number,
                "source_text": m.group(0)[:200],
            }
        )
    # Also catch "u/s 302, 34 IPC"
    alt = re.compile(
        r"[Uu]/[Ss]\s*(?P<secs>[\d,\s\-–and&]+)\s+(?P<act>IPC|BNS|CrPC|BNSS|NDPS|POCSO)",
        re.IGNORECASE,
    )
    for m in alt.finditer(text):
        found.append(
            {
                "act": m.group("act").upper(),
                "section": re.sub(r"\s+", " ", m.group("secs")).strip(" ,"),
                "source_page": page_number,
                "source_text": m.group(0)[:200],
            }
        )
    return found


def extract_person_blocks(text: str, page_number: int, kind: str) -> List[dict]:
    """Heuristic extraction of numbered person entries under accused/witness headings."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    people: List[dict] = []
    capture = False
    buffer: List[str] = []

    # Heading phrases only — do not match body text like "PW-1 ..."
    start_tokens = {
        "accused": [
            "particulars of the accused",
            "particulars of accused",
            "list of accused",
            "list of the accused",
            "accused persons",
            "अभियुक्त",
        ],
        "witness": [
            "particulars of witnesses",
            "particulars of witness",
            "list of witnesses",
            "list of witness",
            "prosecution witnesses",
            "साक्षी",
        ],
    }
    # Exact short headings used on sparse multi-page forms
    exact_headings = {
        "accused": {"accused", "the accused", "accused person", "accused persons"},
        "witness": {"witness", "witnesses", "the witnesses"},
    }
    stop_tokens = [
        "brief facts",
        "course of investigation",
        "details of investigation",
        "property seized",
        "seized property",
        "articles seized",
        "muddemal",
        "acts and sections",
        "conclusion",
        "final conclusion",
        "result of investigation",
        "prayer",
        "list of documents",
        "gist of the case",
    ]
    if kind == "accused":
        stop_tokens += start_tokens["witness"]
    else:
        stop_tokens += start_tokens["accused"]

    def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        block = " ".join(buffer)
        m = re.match(
            r"^(?P<sno>\d{1,3})[\.\)]\s*(?P<body>.+)$",
            block,
            re.IGNORECASE,
        )
        if m:
            body = m.group("body").strip()
            body = re.sub(
                r"^(?:p\.?\s*w\.?|c\.?\s*w\.?)\s*[-–]?\s*\d+\s*",
                "",
                body,
                flags=re.I,
            )
            name = re.split(
                r"\s+(?:s/o|d/o|w/o|so|do|wo|age|aged|r/o)\b",
                body,
                maxsplit=1,
                flags=re.I,
            )[0].strip(" ,.-")
            people.append(
                {
                    "serial_no": m.group("sno"),
                    "name": name or body[:80],
                    "raw": block[:500],
                    "source_page": page_number,
                }
            )
        elif len(block) > 3:
            people.append(
                {
                    "serial_no": None,
                    "name": block[:80],
                    "raw": block[:500],
                    "source_page": page_number,
                }
            )
        buffer = []

    def is_start_heading(line_l: str) -> bool:
        cleaned = line_l.strip(" :.-")
        if cleaned in exact_headings.get(kind, set()):
            return True
        return any(t in line_l for t in start_tokens.get(kind, []))

    def is_stop_heading(line_l: str) -> bool:
        cleaned = line_l.strip(" :.-")
        other = "witness" if kind == "accused" else "accused"
        if cleaned in exact_headings.get(other, set()):
            return True
        return any(t in line_l for t in stop_tokens)

    for line in lines:
        low = line.lower()
        if is_start_heading(low):
            flush()
            capture = True
            if ":" in line:
                _, rest = line.split(":", 1)
                if rest.strip():
                    buffer = [rest.strip()]
                    flush()
            continue
        if capture and is_stop_heading(low):
            flush()
            capture = False
            continue
        if capture:
            if re.match(r"^\d{1,3}[\.\)]\s+", line):
                flush()
                buffer = [line]
            else:
                buffer.append(line)
    if capture:
        flush()
    return people


def dedupe_candidates(candidates: List[dict]) -> Dict[str, dict]:
    """Keep highest-confidence candidate per canonical field."""
    best: Dict[str, dict] = {}
    for c in candidates:
        name = c.get("canonical")
        if not name or c.get("value") in (None, ""):
            continue
        prev = best.get(name)
        if not prev or float(c.get("mapping_confidence") or 0) >= float(
            prev.get("mapping_confidence") or 0
        ):
            best[name] = c
    return best
