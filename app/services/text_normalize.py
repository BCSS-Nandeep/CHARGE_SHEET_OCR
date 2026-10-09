"""General text softening for charge-sheet PDFs with missing spaces / glued tokens.

Not template-specific: applies reversible spacing heuristics only.
"""

from __future__ import annotations

import re

# Common charge-sheet phrase fragments often glued in digital exports
_GLUED_PHRASES = [
    (r"(?i)particularsoftheaccused", "Particulars of the accused"),
    (r"(?i)particularsofthewitnesses", "Particulars of the witnesses"),
    (r"(?i)witnessestobeexamined", "witnesses to be examined"),
    (r"(?i)name\(s\)&particularsoftheaccusedperson\(s\)", "Name(s) & Particulars of the accused person(s)"),
    (r"(?i)nameoftheaccused", "Name of the accused"),
    (r"(?i)listofwitnesses", "List of witnesses"),
    (r"(?i)acts\s*and\s*sections", "Acts and sections"),
    (r"(?i)andsections", "and sections"),
    (r"(?i)typeofinalreport|typeoffinalreport", "Type of final report"),
    (r"(?i)ifsupplementaryororiginal", "If supplementary or original"),
    (r"(?i)chargesheet", "Charge sheet"),
    (r"(?i)policestation", "Police Station"),
    (r"(?i)finalreport", "Final Report"),
    (r"(?i)briefactsofthecase|brieffactsofthecase", "Brief facts of the case"),
    (r"(?i)ndpsact", "NDPS Act"),
    (r"(?i)exciseact", "Excise Act"),
]


def soften_pdf_text(text: str) -> str:
    """Insert likely missing spaces without inventing content."""
    if not text:
        return text
    out = text.replace("\u00a0", " ")

    # Colon/semicolon glued to value
    out = re.sub(r"([:；;])(?=\S)", r"\1 ", out)
    # Letter.Letter → Letter. Letter (Dist.Medak)
    out = re.sub(r"([A-Za-z])\.([A-Z])", r"\1. \2", out)
    # camelCase / PascalCase transitions
    out = re.sub(r"([a-z])([A-Z])", r"\1 \2", out)
    # digit-letter and letter-digit (keep A1 together via later pass)
    out = re.sub(r"(\d)([A-Za-z])", r"\1 \2", out)
    out = re.sub(r"([A-Za-z])(\d)", r"\1 \2", out)
    # Re-glue accused/witness markers A1, A2, LW1
    out = re.sub(r"\b([AALW])\s+(\d+)\b", r"\1\2", out, flags=re.I)
    out = re.sub(r"\b(LW)\s*[-–]?\s*(\d+)\b", r"LW-\2", out, flags=re.I)

    for pat, repl in _GLUED_PHRASES:
        out = re.sub(pat, repl, out)

    # Person attribute markers often glued
    out = re.sub(
        r"(?i)(?<![A-Za-z])(S/o|D/o|W/o|Age|Caste|Occ|R/o|Adhar|Aadhaar|Contact)(?=[A-Za-z0-9])",
        r"\1 ",
        out,
    )
    out = re.sub(
        r"(?i)(?<=[A-Za-z0-9])(S/o|D/o|W/o|Age|Caste|Occ|R/o)(?=[A-Za-z0-9:])",
        r" \1 ",
        out,
    )

    # Normalize whitespace but KEEP 2+ spaces so space-aligned KV columns still split
    out = re.sub(r"[ \t]{2,}", "  ", out)
    out = re.sub(r" *\n *", "\n", out)
    return out.strip()


def looks_like_person_label(label: str) -> bool:
    """True when a pseudo-label is actually a person/narrative fragment."""
    low = (label or "").lower().strip()
    if not low:
        return True
    if re.search(r"\b(?:s/o|d/o|w/o|age\s*:|caste\s*:|occ\s*:|r/o)\b", low):
        return True
    if re.search(r"\b(?:yrs?|years|caste|occupation)\b", low) and len(low) > 20:
        return True
    if len(low.split()) > 8:
        return True
    # Form row endings that are not case-identifier labels
    if re.search(r"\bsheeted\b", low) and "date" not in low and "no" not in low:
        return True
    if re.fullmatch(r"(?:dist\.?\s*)?[a-z.]+\s+p\.?s\.?", low):
        # Combined Dist+PS labels handled separately; reject as atomic label
        return True
    return False
