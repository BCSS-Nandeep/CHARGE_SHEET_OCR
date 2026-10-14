"""Character / word error rates and field coverage — verified comparison only.

These metrics require a reference (ground-truth) string or field map. They never
invent an "accuracy %" without that comparison. Counts are always reported as
``num/den`` so a rate can be audited.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


def _chars(text: str) -> str:
    """Normalize for CER: collapse whitespace, keep letters/digits/punct."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def _words(text: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?|[^\sA-Za-z0-9]", _chars(text))


def levenshtein(a: str, b: str) -> int:
    """Classic edit distance (insert/delete/substitute = 1)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            ins, delete, sub = cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


@dataclass
class Rate:
    errors: int = 0
    reference_units: int = 0

    @property
    def value(self) -> Optional[float]:
        if self.reference_units <= 0:
            return None
        return round(self.errors / self.reference_units, 4)

    def fmt(self) -> str:
        if self.reference_units <= 0:
            return "n/a (no reference)"
        rate = self.errors / self.reference_units
        return f"{rate:.4f} ({self.errors}/{self.reference_units})"


def character_error_rate(reference: str, hypothesis: str) -> Rate:
    """CER = edit_distance(ref, hyp) / len(ref_chars)."""
    ref = _chars(reference)
    hyp = _chars(hypothesis)
    if not ref:
        return Rate(0, 0)
    return Rate(errors=levenshtein(ref, hyp), reference_units=len(ref))


def word_error_rate(reference: str, hypothesis: str) -> Rate:
    """WER = word-level edit distance / len(ref_words)."""
    ref_w = _words(reference)
    hyp_w = _words(hypothesis)
    if not ref_w:
        return Rate(0, 0)
    # SequenceMatcher opcodes → approximate Levenshtein on tokens
    sm = SequenceMatcher(a=ref_w, b=hyp_w)
    errors = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            errors += max(i2 - i1, j2 - j1)
        elif tag == "delete":
            errors += i2 - i1
        elif tag == "insert":
            errors += j2 - j1
    return Rate(errors=errors, reference_units=len(ref_w))


@dataclass
class FieldAccuracy:
    matched: int = 0
    expected: int = 0
    missing: List[str] = field(default_factory=list)
    extra: List[str] = field(default_factory=list)

    @property
    def value(self) -> Optional[float]:
        if self.expected <= 0:
            return None
        return round(self.matched / self.expected, 4)

    def fmt(self) -> str:
        if self.expected <= 0:
            return "n/a (0 expected fields)"
        return f"{self.matched}/{self.expected} matched; missing={len(self.missing)}"


def _norm_label(label: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (label or "").strip().lower())


def field_accuracy(
    expected: Sequence[Tuple[Optional[str], str]],
    extracted: Sequence[Tuple[Optional[str], str]],
) -> FieldAccuracy:
    """Compare (label, value) lists. Values compared with whitespace-insensitive compact form."""
    from app.services.text_normalize import compact

    exp_items = [(_norm_label(l), compact(v), (l, v)) for l, v in expected if compact(v) or l]
    hyp_vals = [compact(v) for _, v in extracted]
    hyp_by_label: Dict[str, List[str]] = {}
    for l, v in extracted:
        hyp_by_label.setdefault(_norm_label(l), []).append(compact(v))

    result = FieldAccuracy(expected=len(exp_items))
    used = [False] * len(hyp_vals)
    for lab, exp_c, (raw_l, raw_v) in exp_items:
        found = False
        if lab and lab in hyp_by_label:
            for hv in hyp_by_label[lab]:
                if not exp_c or exp_c in hv or hv in exp_c:
                    found = True
                    break
        if not found and exp_c:
            for i, hv in enumerate(hyp_vals):
                if used[i]:
                    continue
                if exp_c in hv or hv in exp_c:
                    used[i] = True
                    found = True
                    break
        if found:
            result.matched += 1
        else:
            result.missing.append(f"{raw_l or '(no label)'}: {(raw_v or '')[:80]}")
    return result


@dataclass
class ValidationReport:
    """Verified comparison report — do not treat rates as absolute accuracy claims."""

    document_id: str
    pages_compared: int
    cer: Rate
    wer: Rate
    fields: FieldAccuracy
    missing_content_count: int
    page_notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "pages_compared": self.pages_compared,
            "cer": {"rate": self.cer.value, "detail": self.cer.fmt()},
            "wer": {"rate": self.wer.value, "detail": self.wer.fmt()},
            "field_accuracy": {
                "rate": self.fields.value,
                "detail": self.fields.fmt(),
                "missing_fields": self.fields.missing[:50],
            },
            "missing_content_count": self.missing_content_count,
            "page_notes": self.page_notes,
            "warnings": self.warnings
            + [
                "Rates are only meaningful against the supplied reference; "
                "they are not a general accuracy claim."
            ],
        }

    def to_markdown(self) -> str:
        lines = [
            f"# Validation report — `{self.document_id}`",
            "",
            f"- Pages compared: **{self.pages_compared}**",
            f"- CER: **{self.cer.fmt()}**",
            f"- WER: **{self.wer.fmt()}**",
            f"- Field accuracy: **{self.fields.fmt()}**",
            f"- Missing-content count: **{self.missing_content_count}**",
            "",
            "> Rates require a verified reference. Do not cite a bare accuracy % without this comparison.",
            "",
        ]
        if self.page_notes:
            lines.append("## Page notes")
            lines.extend(f"- {n}" for n in self.page_notes)
            lines.append("")
        if self.fields.missing:
            lines.append("## Missing expected fields (sample)")
            lines.extend(f"- {m}" for m in self.fields.missing[:30])
            lines.append("")
        if self.warnings:
            lines.append("## Warnings")
            lines.extend(f"- {w}" for w in self.warnings)
            lines.append("")
        return "\n".join(lines)


def build_validation_report(
    *,
    document_id: str,
    reference_text: str,
    hypothesis_text: str,
    expected_fields: Sequence[Tuple[Optional[str], str]] = (),
    extracted_fields: Sequence[Tuple[Optional[str], str]] = (),
    pages_compared: int = 1,
    page_notes: Optional[Iterable[str]] = None,
    warnings: Optional[Iterable[str]] = None,
) -> ValidationReport:
    cer = character_error_rate(reference_text, hypothesis_text)
    wer = word_error_rate(reference_text, hypothesis_text)
    fields = field_accuracy(expected_fields, extracted_fields)
    return ValidationReport(
        document_id=document_id,
        pages_compared=pages_compared,
        cer=cer,
        wer=wer,
        fields=fields,
        missing_content_count=len(fields.missing),
        page_notes=list(page_notes or []),
        warnings=list(warnings or []),
    )
