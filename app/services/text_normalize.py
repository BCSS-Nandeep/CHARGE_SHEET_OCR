"""General text softening for charge-sheet PDFs with missing spaces / glued tokens.

Not template-specific: applies reversible spacing heuristics only.
"""

from __future__ import annotations

import re
import unicodedata

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
    (r"(?i)\bSIof(?=[A-Z\s])", "SI of "),
]


def strip_accents(text: str) -> str:
    """OCR often adds diacritics to Latin text ("Áge"); fold them away."""
    if not text or text.isascii():
        return text
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def soften_pdf_text(text: str) -> str:
    """Insert likely missing spaces without inventing content."""
    if not text:
        return text
    out = strip_accents(text.replace(" ", " ")).replace("’", "'").replace("‘", "'")

    # Colon/semicolon glued to value
    out = re.sub(r"([:；;])(?=\S)", r"\1 ", out)
    # Word.Word → Word. Word (Dist.Northtown, J.Doe) but keep abbreviations
    # made of single letters (I.O., P.S., A.P., Cr.P.C.)
    out = re.sub(r"([A-Za-z])\.([A-Z][a-z])", r"\1. \2", out)
    # Label.Number → Label. Number (Dt.02.03.2017, No.4/2017, Rs.500)
    out = re.sub(r"([A-Za-z]{2,})\.(\d)", r"\1. \2", out)
    # camelCase / PascalCase transitions
    out = re.sub(r"([a-z])([A-Z])", r"\1 \2", out)
    # digit-letter and letter-digit (keep A1 together via later pass)
    out = re.sub(r"(\d)([A-Za-z])", r"\1 \2", out)
    out = re.sub(r"([A-Za-z])(\d)", r"\1 \2", out)
    # Re-glue accused/witness markers A1, A2, LW1
    out = re.sub(r"\b([AALW])\s+(\d+)\b", r"\1\2", out, flags=re.I)
    out = re.sub(r"\b(LW)\s*[-–]?\s*(\d+)\b", r"LW-\2", out, flags=re.I)
    # Re-glue ordinal/unit suffixes split above ("1 st", "45 yrs" stays spaced)
    out = re.sub(r"\b(\d+)\s+(st|nd|rd|th)\b", r"\1\2", out)

    for pat, repl in _GLUED_PHRASES:
        out = re.sub(pat, repl, out)

    # Person attribute markers often glued ("S/osailu", "KiranS/o", "sikhocc:labour").
    # Slash markers are distinctive; word markers (Age/Caste/Occ) only split when
    # followed by a capital, digit or colon so "Occurred", "occupied", "Manager",
    # "Villagers" are left intact.
    out = re.sub(r"(?i)(?<![A-Za-z])([SDWCR]/o)(?=[A-Za-z0-9])", r"\1 ", out)
    out = re.sub(r"(?i)(?<=[A-Za-z0-9])([SDWR]/o)(?=[A-Za-z0-9:])", r" \1 ", out)
    out = re.sub(r"(?<![A-Za-z])((?i:age|caste|occ|adhar|aadhaar|contact))(?=[A-Z0-9])", r"\1 ", out)
    out = re.sub(r"(?<=[A-Za-z0-9])((?i:age|caste|occ))(?=[A-Z0-9:])", r" \1 ", out)

    # Normalize whitespace but KEEP 2+ spaces so space-aligned KV columns still split
    out = re.sub(r"[ \t]{2,}", "  ", out)
    out = re.sub(r" *\n *", "\n", out)
    return out.strip()


_ABBREVIATIONS = {
    "no", "nos", "sri", "smt", "shri", "dr", "mr", "mrs", "ms", "cr", "rs", "dt", "vs", "sec", "st",
    "ps", "hon", "md", "ltd", "co", "lr", "cpr", "fir", "occ", "ph", "vide", "viz", "etc",
}
_SENTENCE_END = re.compile(r"(\S+?)\.[\"”']?\s+(?=[A-Z(\"“])")


def _sentence_ends(text: str):
    for m in _SENTENCE_END.finditer(text):
        word = re.sub(r"[^A-Za-z]", "", m.group(1).split(".")[-1]).lower()
        if len(word) > 1 and word not in _ABBREVIATIONS:
            yield m.end()


def sentence_bounds(text: str, pos: int, max_len: int = 700) -> tuple:
    """(start, end) of the sentence containing ``pos``; 'Cr. No.' / 'Sri.' don't end sentences."""
    start, end = max(0, pos - max_len), min(len(text), pos + max_len)
    for e in _sentence_ends(text[start:end]):
        if start + e <= pos:
            continue
        end = start + e
        break
    for e in _sentence_ends(text[max(0, pos - max_len) : pos]):
        start = max(0, pos - max_len) + e
    return start, end


def compact(text: str) -> str:
    """Lowercase letters/digits only — matches glued and spaced variants alike."""
    return re.sub(r"[^a-z0-9]", "", strip_accents(text or "").lower())


def clean_value(text: str) -> str:
    """Collapse whitespace and trim list/sentence punctuation around a value."""
    v = re.sub(r"\s+", " ", (text or "").replace(" ", " ")).strip()
    return v.strip(" ,;:.-–—").strip()


def looks_like_person_label(label: str) -> bool:
    """True when a pseudo-label is actually a person/narrative fragment."""
    low = (label or "").lower().strip()
    if not low:
        return True
    if re.search(r"\b(?:s/o|d/o|w/o|age\s*:|caste\s*:|occ\s*:|r/o)\b", low):
        return True
    if re.search(r"\b(?:yrs?|years|caste|occupation)\b", low) and len(low) > 20:
        return True
    # Form row endings that are not case-identifier labels
    if re.search(r"\bsheeted\b", low) and "date" not in low and "no" not in low:
        return True
    if re.fullmatch(r"(?:dist\.?\s*)?[a-z.]+\s+p\.?s\.?", low):
        # Combined Dist+PS labels handled separately; reject as atomic label
        return True
    return False
