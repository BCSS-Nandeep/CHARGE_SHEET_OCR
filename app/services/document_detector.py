"""Template-independent document type detection and fingerprinting."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.services.field_synonyms import DOCUMENT_TYPE_KEYWORDS, SECTION_HEADER_HINTS
from app.services.field_mapping_service import normalize_label


@dataclass
class DetectionResult:
    document_type: str
    confidence: float
    fingerprint: str
    notes: str
    section_hits: List[str]
    uncertain: bool


class DocumentDetector:
    def detect(self, full_text: str, labels: Optional[List[str]] = None) -> DetectionResult:
        text_l = (full_text or "").lower()
        hits = [kw for kw in DOCUMENT_TYPE_KEYWORDS if kw in text_l]
        section_hits = [h for h in SECTION_HEADER_HINTS if h in text_l]

        score = 0.0
        if hits:
            score += min(0.55, 0.2 * len(hits))
        if section_hits:
            score += min(0.35, 0.08 * len(section_hits))

        # Presence of FIR / PS / accused patterns
        if re.search(r"\bfir\b|\bf\.i\.r\b", text_l):
            score += 0.1
        if re.search(r"\baccused\b|\bwitness", text_l):
            score += 0.1

        score = min(score, 0.99)
        doc_type = "charge_sheet" if score >= 0.35 else "unknown"
        uncertain = score < 0.55 or doc_type == "unknown"

        notes = (
            f"keyword_hits={len(hits)}; section_hits={len(section_hits)}; "
            f"type={doc_type}"
        )
        fingerprint = self.fingerprint(full_text, labels or [])
        return DetectionResult(
            document_type=doc_type,
            confidence=round(score, 3),
            fingerprint=fingerprint,
            notes=notes,
            section_hits=section_hits,
            uncertain=uncertain,
        )

    @staticmethod
    def fingerprint(full_text: str, labels: List[str]) -> str:
        """Stable structural fingerprint from normalized labels / headings."""
        tokens: List[str] = []
        for lab in labels:
            n = normalize_label(lab)
            if n and len(n) >= 3:
                tokens.append(n)
        if not tokens:
            # Fall back to first non-empty lines as pseudo-headings
            for line in (full_text or "").splitlines():
                line = normalize_label(line)
                if 3 <= len(line) <= 60:
                    tokens.append(line)
                if len(tokens) >= 40:
                    break
        tokens = sorted(set(tokens))[:60]
        blob = "|".join(tokens)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def similarity(self, fp_a: str, fp_b: str) -> float:
        if not fp_a or not fp_b:
            return 0.0
        if fp_a == fp_b:
            return 1.0
        # Hex fingerprints: compare prefix overlap as weak similarity
        same = sum(1 for a, b in zip(fp_a, fp_b) if a == b)
        return same / max(len(fp_a), len(fp_b))
