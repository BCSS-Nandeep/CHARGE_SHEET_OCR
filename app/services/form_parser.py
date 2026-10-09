"""Stage: layout / form-column parsing for Indian charge-sheet PDFs.

Maps by label meaning and local context. Form numbers are hints only —
never sole authority for field meaning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.services.field_mapping_service import best_canonical_match, normalize_label

_WS = re.compile(r"\s+")
_FIR_INLINE = re.compile(
    r"(?:FIR\.?\s*No\.?|F\.I\.R\.?\s*No\.?|Cr\.?\s*No\.?|Crime\s*No\.?)\s*[:\-]?\s*([0-9]+/[0-9]{2,4})",
    re.I,
)
_YEAR_ONLY = re.compile(r"\b((?:19|20)\d{2})\b")
_DATE = re.compile(
    r"(?:Dt\.?\s*)?(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4})",
    re.I,
)
_FORM_START = re.compile(r"^\s*0?(\d{1,2})[\.\)]?\s+(.+)$", re.I)


@dataclass
class RawCandidate:
    label: str
    value: str
    canonical: Optional[str]
    mapping_confidence: float
    source_page: int
    source_text: str
    form_hint: Optional[str] = None  # e.g. "col_01"


@dataclass
class FormParseResult:
    candidates: List[RawCandidate] = field(default_factory=list)
    section_blocks: Dict[str, str] = field(default_factory=dict)  # logical block name -> text
    page_texts: Dict[int, str] = field(default_factory=dict)


def _clean(text: str) -> str:
    return _WS.sub(" ", (text or "").replace("\u00a0", " ")).strip()


def expand_compound_value(label: str, value: str, page: int, source_text: str) -> List[RawCandidate]:
    """Split compound year/FIR lines into separate fields (layout-agnostic)."""
    out: List[RawCandidate] = []
    label_n = normalize_label(label)
    value_c = _clean(value)
    if not value_c:
        return out

    # Year line that also embeds FIR number
    if "year" in label_n or label_n == "year":
        fir = _FIR_INLINE.search(value_c)
        year_m = _YEAR_ONLY.search(value_c)
        if year_m:
            out.append(
                RawCandidate(
                    label=label,
                    value=year_m.group(1),
                    canonical="year",
                    mapping_confidence=0.98,
                    source_page=page,
                    source_text=source_text[:300],
                )
            )
        if fir:
            out.append(
                RawCandidate(
                    label="FIR No.",
                    value=fir.group(1),
                    canonical="fir_number",
                    mapping_confidence=0.95,
                    source_page=page,
                    source_text=source_text[:300],
                )
            )
        if out:
            return out

    # Standalone FIR / crime in value of another field
    fir = _FIR_INLINE.search(f"{label} {value_c}")
    if fir and "fir" not in label_n and "crime" not in label_n:
        # still map primary below; add FIR as extra
        pass

    canon, score = best_canonical_match(label)
    if canon and score >= 0.72 and value_c not in {"-", "—", "---", "."}:
        out.append(
            RawCandidate(
                label=label,
                value=value_c,
                canonical=canon,
                mapping_confidence=score,
                source_page=page,
                source_text=source_text[:300],
            )
        )
    elif value_c not in {"-", "—", "---", "."}:
        out.append(
            RawCandidate(
                label=label,
                value=value_c,
                canonical=None,
                mapping_confidence=score,
                source_page=page,
                source_text=source_text[:300],
            )
        )

    # Extra FIR extraction from same source line
    fir = _FIR_INLINE.search(source_text)
    if fir and not any(c.canonical == "fir_number" for c in out):
        out.append(
            RawCandidate(
                label="FIR No.",
                value=fir.group(1),
                canonical="fir_number",
                mapping_confidence=0.9,
                source_page=page,
                source_text=source_text[:300],
            )
        )

    # Date lines: "Dt.05.05.2020." near year/FIR header
    if re.search(r"^\s*Dt\.?\s*\d", value_c, re.I) or (
        label_n in {"", "dt"} and _DATE.search(value_c)
    ):
        dm = _DATE.search(value_c)
        if dm:
            out.append(
                RawCandidate(
                    label="Date of FIR",
                    value=dm.group(1),
                    canonical="fir_date",
                    mapping_confidence=0.85,
                    source_page=page,
                    source_text=source_text[:300],
                )
            )

    return out


def parse_inline_kv_lines(text: str, page: int) -> List[RawCandidate]:
    """Parse 'Label: value' occurrences including multiple on one line."""
    candidates: List[RawCandidate] = []
    lines = [ln.rstrip() for ln in text.splitlines()]
    i = 0
    while i < len(lines):
        raw = lines[i].strip()
        if not raw:
            i += 1
            continue
        m = _FORM_START.match(raw)
        form_hint = None
        body = raw
        if m:
            form_hint = f"col_{int(m.group(1)):02d}"
            body = m.group(2).strip()

        # Date-only line under FIR header
        if re.match(r"^Dt\.?\s*\d", body, re.I):
            dm = _DATE.search(body)
            if dm:
                candidates.append(
                    RawCandidate(
                        label="Date of FIR",
                        value=dm.group(1),
                        canonical="fir_date",
                        mapping_confidence=0.88,
                        source_page=page,
                        source_text=raw[:300],
                        form_hint=form_hint,
                    )
                )
            i += 1
            continue

        # Combined Dist + PS on one line: "Dist. Medak PS: HAVELIGHANAPUR"
        dist_ps = re.match(
            r"(?i)^(?:0?\d+[\.\)]?\s*)?dist(?:rict|\.)?\s*([A-Za-z.]+)\s+P\.?S\.?\s*:\s*(.+)$",
            body,
        )
        if dist_ps:
            candidates.append(
                RawCandidate(
                    label="District",
                    value=_clean(dist_ps.group(1)),
                    canonical="district",
                    mapping_confidence=0.96,
                    source_page=page,
                    source_text=raw[:300],
                    form_hint=form_hint,
                )
            )
            candidates.append(
                RawCandidate(
                    label="Police Station",
                    value=_clean(dist_ps.group(2)),
                    canonical="police_station",
                    mapping_confidence=0.96,
                    source_page=page,
                    source_text=raw[:300],
                    form_hint=form_hint,
                )
            )
            i += 1
            continue

        # Multi-KV only for clear Capitalized Label: patterns (avoid splitting "No.:")
        label_starts = list(
            re.finditer(r"(?:(?<=^)|(?<=\s))([A-Z][A-Za-z][A-Za-z ]{1,40}?)\s*:", body)
        )
        if len(label_starts) >= 2:
            parts = []
            for idx, m in enumerate(label_starts):
                end = label_starts[idx + 1].start() if idx + 1 < len(label_starts) else len(body)
                parts.append(body[m.start() : end].strip())
        else:
            parts = [body]

        produced = False
        for part in parts:
            if ":" not in part:
                sp = re.split(r"\s{2,}", part, maxsplit=1)
                if len(sp) == 2 and sp[1].strip():
                    label, value = sp[0].strip(), sp[1].strip()
                else:
                    # Only known multi-line person labels (IO / complainant), not every heading
                    label_only = re.sub(r"[\.,;:]+$", "", part).strip()
                    label_l = label_only.lower()
                    allow_multiline = any(
                        k in label_l
                        for k in (
                            "name and rank of i.o",
                            "name of complainant",
                            "or informant",
                            "investigating officer",
                        )
                    )
                    canon, score = best_canonical_match(label_only)
                    if allow_multiline and canon in {"io_name", "complainant_name", "informant_name"} and score >= 0.75:
                        nxt = []
                        j = i + 1
                        while j < len(lines) and len(nxt) < 2:
                            nxt_line = lines[j].strip()
                            if not nxt_line:
                                j += 1
                                continue
                            if _FORM_START.match(nxt_line):
                                break
                            if re.match(r"^\d{1,2}\s+\w", nxt_line):
                                break
                            nxt.append(nxt_line)
                            j += 1
                        if nxt:
                            value = " ".join(nxt)
                            expanded = expand_compound_value(label_only, value, page, raw + " " + value)
                            for c in expanded:
                                c.form_hint = form_hint
                            candidates.extend(expanded)
                            produced = True
                    continue
            else:
                label, value = part.split(":", 1)
                label, value = label.strip(), value.strip()
            if not label or len(label) > 80:
                continue
            if len(label.split()) > 10:
                continue
            # Pull continuation line if value empty
            if not value or value in {"-", "—"}:
                if i + 1 < len(lines):
                    nxt = lines[i + 1].strip()
                    if nxt and not _FORM_START.match(nxt) and ":" not in nxt[:20]:
                        value = nxt
            expanded = expand_compound_value(label, value, page, raw)
            for c in expanded:
                c.form_hint = form_hint
            candidates.extend(expanded)
            produced = True

        if re.search(r"dated", body, re.I) and _DATE.search(body):
            dm = _DATE.search(body)
            if dm:
                candidates.append(
                    RawCandidate(
                        label="Dated",
                        value=dm.group(1),
                        canonical="submission_date",
                        mapping_confidence=0.9,
                        source_page=page,
                        source_text=raw[:300],
                        form_hint=form_hint,
                    )
                )
        i += 1
    return candidates


def extract_section_blocks(full_text: str) -> Dict[str, str]:
    """Split document into logical blocks by heading meaning."""
    patterns = [
        (
            "accused_charged",
            r"(?:name\s*(?:\(s\))?\s*&?\s*particulars\s+of\s+the\s+accused|"
            r"name\s+of\s+accused\s+person\(s\)\s*charge\s*sheeted|"
            r"particulars\s+of\s+the\s+accused|list\s+of\s+accused)",
        ),
        (
            "accused_not_charged",
            r"(?:name\s+of\s+accused\s+persons?\(s\)\s*not\s+charge\s*sheeted|"
            r"particulars\s+of\s+the\s+accused\s+not\s+charge\s*sheeted)",
        ),
        (
            "witnesses",
            r"(?:list\s+of\s+witnesses|witnesses\s+to\s+be\s+examined|"
            r"particulars\s+of\s+(?:the\s+)?witness)",
        ),
        ("property", r"(?:details\s+of\s+(?:the\s+)?property|property\s+recovered|property\s+seized|muddemal)"),
        ("lab", r"(?:result\s+of\s+laboratory|fsl|chemical\s+analysis)"),
        ("brief_facts", r"(?:brief\s+facts\s+of\s+the\s+case|facts\s+of\s+the\s+case|gist\s+of\s+the\s+case)"),
        ("investigation", r"(?:course\s+of\s+investigation|during\s+the\s+course\s+of\s+investigation)"),
        ("acts", r"(?:^|\n)\s*0?4\s+acts?(?:\s+and\s+sections)?|(?:acts?\s+and\s+sections|sections?\s*:)"),
    ]
    lower = full_text.lower()
    hits: List[Tuple[int, str]] = []
    for name, pat in patterns:
        m = re.search(pat, lower, re.I)
        if m:
            hits.append((m.start(), name))
    hits.sort()
    blocks: Dict[str, str] = {}
    for i, (start, name) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(full_text)
        chunk = full_text[start:end].strip()
        # Avoid tiny heading-only chunks
        if len(chunk) > 20:
            blocks[name] = chunk[:8000]
    return blocks


def parse_document_pages(pages: List[Tuple[int, str]]) -> FormParseResult:
    from app.services.text_normalize import soften_pdf_text

    result = FormParseResult()
    full_parts = []
    for page_no, text in pages:
        soft = soften_pdf_text(text)
        result.page_texts[page_no] = soft
        full_parts.append(f"--- PAGE {page_no} ---\n{soft}")
        result.candidates.extend(parse_inline_kv_lines(soft, page_no))
        # Compound header: "Dist. Medak PS: HAVELIGHANAPUR"
        for m in re.finditer(
            r"(?i)\bdist(?:rict|\.)?\s*([A-Za-z. ]{2,40}?)\s+P\.?S\.?\s*:\s*([A-Za-z0-9. ]{2,60})",
            soft,
        ):
            result.candidates.append(
                RawCandidate(
                    label="District",
                    value=_clean(m.group(1)),
                    canonical="district",
                    mapping_confidence=0.95,
                    source_page=page_no,
                    source_text=m.group(0)[:300],
                )
            )
            result.candidates.append(
                RawCandidate(
                    label="Police Station",
                    value=_clean(m.group(2)),
                    canonical="police_station",
                    mapping_confidence=0.95,
                    source_page=page_no,
                    source_text=m.group(0)[:300],
                )
            )
        for m in re.finditer(
            r"(?i)\bFIR\.?\s*No\.?\s*[:\-]?\s*([0-9]+/[0-9]{2,4})",
            soft,
        ):
            result.candidates.append(
                RawCandidate(
                    label="FIR No.",
                    value=m.group(1),
                    canonical="fir_number",
                    mapping_confidence=0.95,
                    source_page=page_no,
                    source_text=m.group(0)[:300],
                )
            )
    full = "\n\n".join(full_parts)
    result.section_blocks = extract_section_blocks(full)
    return result


def _value_quality(canonical: str, value: str) -> float:
    """Prefer clean short values; reject narrative/phone bleed into case fields."""
    v = (value or "").strip()
    if not v:
        return -1.0
    score = 1.0
    if canonical in {"district", "police_station", "year", "fir_number", "fir_date", "submission_date"}:
        if len(v) > 60:
            score -= 0.8
        if re.search(
            r"\b(?:committed|accused|toddy|investigation|land owner|witness|panch|r/o)\b",
            v,
            re.I,
        ):
            score -= 0.9
        if re.fullmatch(r"\d{10}", v):
            score -= 1.0
        if canonical == "year":
            if re.fullmatch(r"(19|20)\d{2}", v):
                score += 0.8
            elif not re.search(r"(19|20)\d{2}", v):
                score -= 1.0
            else:
                score -= 0.2
        if canonical == "fir_number" and re.search(r"\d+/\d{2,4}", v):
            score += 0.4
        if canonical in {"fir_date", "submission_date"}:
            if not re.fullmatch(
                r"(?:Dt\.?\s*)?\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}\.?",
                v,
                re.I,
            ):
                score -= 1.0
            else:
                score += 0.5
        if canonical == "district":
            if re.search(r"\d{5,}", v) or len(v.split()) > 4:
                score -= 1.0
            if re.search(r"(?i)\b(?:ps|police|station|haveli)\b", v) and "dist" not in v.lower():
                # PS name bled into district
                score -= 0.5
            if re.fullmatch(r"[A-Za-z. ]{3,40}", v):
                score += 0.3
            # Prefer short district tokens (Medak) over long PS names
            if len(v) <= 20:
                score += 0.2
        if canonical == "police_station":
            if re.fullmatch(r"I\.?O\.?", v, re.I) or len(v) < 3:
                score -= 1.0
        if canonical == "submission_date":
            if re.search(r"(?i)sheeted|accused|witness", v) or not _DATE.search(v):
                score -= 1.0
    return score


def dedupe_raw(candidates: List[RawCandidate]) -> Dict[str, RawCandidate]:
    """Keep best mapped candidate per canonical; preserve conflicts separately later."""
    best: Dict[str, RawCandidate] = {}
    for c in candidates:
        if not c.canonical or not c.value:
            continue
        q = _value_quality(c.canonical, c.value)
        if q < 0:
            continue
        prev = best.get(c.canonical)
        rank = (q, c.mapping_confidence)
        if not prev:
            best[c.canonical] = c
            continue
        prev_rank = (_value_quality(prev.canonical, prev.value), prev.mapping_confidence)
        if rank >= prev_rank:
            best[c.canonical] = c
    return best
