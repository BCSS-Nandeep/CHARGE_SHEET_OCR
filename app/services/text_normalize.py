"""Text softening / spacing repair for PDF OCR and native DOCX.

Two complementary passes (never invent words — only insert/remove spaces):

1. ``restore_missing_spaces`` — split glued prose (names, dates, quantities,
   common English/legal phrases).
2. ``repair_spacing_artifacts`` — remove false gaps inside identifiers
   (P8, M11, 07:30, chassis/engine codes). Does **not** glue quantity units
   (``6 kgs``, ``600 grams`` stay spaced).
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# Common charge-sheet / IR phrase fragments often glued in digital exports
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
    (r"(?i)outofthe", "Out of the"),
    (r"(?i)lateron", "Later on"),
    (r"(?i)gramsof", "grams of"),
    (r"(?i)dayssago|daysago", "days ago"),
]

# Quantity / measure words — always prefer a space before these when glued to digits
_UNIT_WORDS = (
    "grams", "gram", "kgs", "kg", "hours", "hour", "hrs", "hr",
    "yrs", "yr", "years", "year", "days", "day", "km", "cms", "cm", "mms", "mm", "fts", "ft",
)


def strip_accents(text: str) -> str:
    """OCR often adds diacritics to Latin text ("Áge"); fold them away."""
    if not text or text.isascii():
        return text
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


@lru_cache(maxsize=1)
def _lexicon() -> tuple:
    """Longest-first common words for conservative glued-lowercase segmentation."""
    words = {
        "previously", "particulars", "interrogation", "facilitators", "associates",
        "about", "later", "since", "after", "before", "during", "while", "where",
        "grams", "gram", "ganja", "hours", "hour", "days", "day", "ago", "sold",
        "packet", "seized", "cover", "put", "was", "were", "been", "being",
        "out", "of", "the", "and", "to", "one", "for", "from", "with", "into",
        "onto", "over", "under", "near", "him", "her", "his", "their", "them",
        "this", "that", "these", "those", "then", "than", "when", "whom",
        "police", "station", "accused", "witness", "officer", "report",
        "at", "on", "in", "by", "an", "as", "or", "is", "are", "be",
        "kgs", "kg", "yrs", "yr", "hrs", "hr",
        # Note: bare "a" / "i" omitted — they over-segment names (Ganj|a|toone)
    }
    return tuple(sorted(words, key=len, reverse=True))


def _segment_glued_lowercase(token: str) -> str:
    """Split a long lowercase (or lower+digit) glued token using a small lexicon.

    Only rearranges spaces; never changes letters. If segmentation fails, returns
    the token unchanged.
    """
    if len(token) < 4 or not re.fullmatch(r"[a-z0-9]+", token):
        return token
    lex = _lexicon()
    parts: list[str] = []
    i = 0
    n = len(token)
    while i < n:
        if token[i].isdigit():
            j = i
            while j < n and token[j].isdigit():
                j += 1
            parts.append(token[i:j])
            i = j
            continue
        matched = False
        for w in lex:
            if not token.startswith(w, i):
                continue
            end = i + len(w)
            # Accept when rest is empty, a digit, or continues with another lexicon word
            rest = token[end:]
            if end == n or (rest and rest[0].isdigit()) or (
                rest and any(rest.startswith(x) for x in lex)
            ):
                parts.append(w)
                i = end
                matched = True
                break
        if not matched:
            return token  # do not invent mid-name splits
    return " ".join(parts) if parts else token


def restore_missing_spaces(text: str) -> str:
    """Insert spaces that OCR/PDF glue removed. Letters/digits unchanged."""
    if not text:
        return text
    out = text

    for pat, repl in _GLUED_PHRASES:
        out = re.sub(pat, repl, out)

    # Period glued to next word: packet.seized / him.Previosuly (no spelling change)
    out = re.sub(r"([a-z])\.([A-Za-z])", r"\1. \2", out)
    # camelCase / PascalCase: DosapatiKalyan, isDosapati
    out = re.sub(r"([a-z])([A-Z])", r"\1 \2", out)
    # Date then word: 26.08.2025atabout → 26.08.2025 atabout
    out = re.sub(r"(\d{1,2}\.\d{1,2}\.\d{2,4})(?=[A-Za-z])", r"\1 ", out)
    # Time then word: 14:30hours → 14:30 hours
    out = re.sub(r"(\d{1,2}:\d{2})(?=[A-Za-z])", r"\1 ", out)
    # Peel common verbs/prepositions glued after a name: Kalyansold → Kalyan sold
    out = re.sub(
        r"(?<=[a-z])(sold|about|since|later|was|were|from|with|into|onto)(?=\d|[A-Z]|$)",
        r" \1",
        out,
        flags=re.I,
    )
    # Digit then lowercase (quantities/prose): 6kgs, 600grams, 3days.
    # Digit then Title-case unit/name: 200Grams, 6Kgs
    # Do NOT split digit↔ALLCAPS runs (chassis MD634KE).
    out = re.sub(r"(\d)([a-z])", r"\1 \2", out)
    out = re.sub(r"(\d)([A-Z][a-z]{2,})", r"\1 \2", out)
    # Lowercase then digit: since3, ago10 — not KE44 / P8
    out = re.sub(r"([a-z])(\d)", r"\1 \2", out)
    # Quantity notation: 1K,G / 6K,Gs
    out = re.sub(r"\b(\d)\s*([Kk],\s*[Gg]s?)\b", r"\1 \2", out)
    out = re.sub(r"\b(\d)([Kk],\s*[Gg]s?)\b", r"\1 \2", out)

    # Lexicon segmentation on long glued lowercase runs still left
    def _seg_match(m: re.Match) -> str:
        return _segment_glued_lowercase(m.group(0))

    out = re.sub(r"\b[a-z]{6,}\b", _seg_match, out)
    # Title-case word glued to lowercase tail: Ganjatoone → Ganja to one
    out = re.sub(r"\b[A-Z][a-z]{5,}\b", _peel_title_candidate, out)

    return out


def _peel_title_candidate(m: re.Match) -> str:
    """Try split points so the lowercase tail lexicon-segments (longest head first)."""
    token = m.group(0)
    # Prefer Ganja|toone over Ganj|atoone (longest Title-case head wins)
    for i in range(len(token) - 3, 1, -1):
        head, tail = token[:i], token[i:]
        if not (head[0].isupper() and head[1:].islower() and tail.islower() and len(tail) >= 4):
            continue
        seg = _segment_glued_lowercase(tail)
        if seg != tail:
            return f"{head} {seg}"
    return token


def repair_spacing_artifacts(text: str) -> str:
    """Remove incorrect spaces inside identifiers; keep real word/unit spaces."""
    if not text:
        return text
    out = text

    # Times: 07: 30 → 07:30
    out = re.sub(r"\b(\d{1,2}):\s+(\d{2})\b", r"\1:\2", out)
    out = re.sub(r"\b(\d{1,2}:\d{2}):\s+(\d{2})\b", r"\1:\2", out)

    # Multi-letter markers first: LW 2 → LW-2
    out = re.sub(r"\b(LW|PW|MO)\s*[-–]?\s*(\d{1,3})\b", r"\1-\2", out, flags=re.I)
    # Evidence / accused markers: P 8 → P8, M 11 → M11, A 1 → A1
    out = re.sub(r"\b([PMALW])\s+(\d{1,3})\b", r"\1\2", out, flags=re.I)

    # Short uppercase codes: PC. 1460 → PC.1460
    out = re.sub(r"\b([A-Z]{2,5})\.\s+(\d[\dA-Za-z/-]*)\b", r"\1.\2", out)

    # OCR plural split before slash: contact s/facilitators (lowercase s only)
    out = re.sub(r"([A-Za-z]{2,})\s+(s)(/)", r"\1\2\3", out)

    # Initials with too many spaces: G.  Srinivas → G. Srinivas
    out = re.sub(r"\b([A-Z])\.\s{2,}([A-Z])", r"\1. \2", out)

    # Chassis / engine IDs
    out = _collapse_alnum_id_runs(out)
    out = _join_adjacent_id_tokens(out)

    # Path/slug fragments next to URL punctuation only
    out = re.sub(
        r"(?<=[./])([a-z]{2,12}(?:\s+[a-z]{2,12}){1,4})(?=[./]|$)",
        lambda m: m.group(1).replace(" ", ""),
        out,
    )

    return out


def _collapse_alnum_id_runs(text: str) -> str:
    """Join spaced alphanumeric ID runs (chassis/engine), leave normal phrases."""

    def repl(m: re.Match) -> str:
        raw = m.group(0)
        if " " not in raw:
            return raw
        compact_id = re.sub(r"\s+", "", raw)
        if len(compact_id) < 8 or not re.fullmatch(r"[A-Za-z0-9]+", compact_id):
            return raw
        letters = re.findall(r"[A-Za-z]+", compact_id)
        digits = re.findall(r"\d+", compact_id)
        chunks = raw.split()

        def _chunk_ok(c: str) -> bool:
            if c.isdigit():
                return len(c) <= 12
            return c.isalpha() and len(c) <= 6

        if not chunks or not all(_chunk_ok(c) for c in chunks):
            return raw
        # Prose dates/times ("2017 at 1500 hrs") — never collapse
        stop = {
            "no", "nos", "fir", "cr", "ps", "case", "act", "at", "on", "of", "to",
            "in", "by", "or", "and", "the", "a", "an", "hrs", "hr", "yrs", "yr",
            "kg", "kgs", "gram", "grams", "hours", "hour", "days", "day",
        }
        if any(c.lower() in stop for c in chunks):
            return raw
        # Chassis/engine IDs are uppercase letter groups, not lowercase words
        alpha_chunks = [c for c in chunks if c.isalpha()]
        if alpha_chunks and any(c.islower() for c in alpha_chunks):
            return raw
        if len(chunks) >= 3 and len(letters) >= 1 and len(digits) >= 1:
            return compact_id
        if len(letters) >= 2 and len(digits) >= 2:
            return compact_id
        return raw

    return re.sub(
        r"\b(?:[A-Za-z]{1,6}|\d{1,12})(?:\s+(?:[A-Za-z]{1,6}|\d{1,12})){2,}\b",
        repl,
        text,
    )


def _join_adjacent_id_tokens(text: str) -> str:
    """Join leftover ID halves: MD634KE44B2 A58303 → MD634KE44B2A58303."""
    prev = None
    out = text
    while prev != out:
        prev = out
        out = re.sub(
            r"\b([A-Z][A-Z0-9]{5,})\s+([A-Z0-9]{4,})\b",
            lambda m: m.group(1) + m.group(2)
            if (re.search(r"\d", m.group(1)) and re.search(r"\d", m.group(2)))
            else m.group(0),
            out,
        )
    return out


def normalize_doc_text(text: str) -> str:
    """Normalize DOCX/native text: restore missing spaces, repair false ID gaps."""
    if not text:
        return text
    out = strip_accents(text.replace("\u00a0", " ")).replace("\u2019", "'").replace("\u2018", "'")
    out = restore_missing_spaces(out)
    out = repair_spacing_artifacts(out)
    out = re.sub(r"[ \t]{3,}", "  ", out)
    out = re.sub(r" *\n *", "\n", out)
    return out.strip()


def soften_pdf_text(text: str) -> str:
    """Insert likely missing spaces in glued PDFs, then repair false ID gaps."""
    if not text:
        return text
    out = strip_accents(text.replace("\u00a0", " ")).replace("\u2019", "'").replace("\u2018", "'")

    # Colon/semicolon glued to value — but never break times (07:30)
    out = re.sub(r"([；;])(?=\S)", r"\1 ", out)
    out = re.sub(r":(?!\d)(?=\S)", ": ", out)
    # Single-letter abbreviations: I.O., P.S. — only split x.Word
    out = re.sub(r"([A-Za-z])\.([A-Z][a-z])", r"\1. \2", out)
    out = re.sub(r"([A-Za-z]{2,})\.(\d)", r"\1. \2", out)

    out = restore_missing_spaces(out)

    # Re-glue ordinal suffixes ("1 st")
    out = re.sub(r"\b(\d+)\s+(st|nd|rd|th)\b", r"\1\2", out)

    # Person attribute markers often glued
    out = re.sub(r"(?i)(?<![A-Za-z])([SDWCR]/o)(?=[A-Za-z0-9])", r"\1 ", out)
    out = re.sub(r"(?i)(?<=[A-Za-z0-9])([SDWR]/o)(?=[A-Za-z0-9:])", r" \1 ", out)
    out = re.sub(r"(?<![A-Za-z])((?i:age|caste|occ|adhar|aadhaar|contact))(?=[A-Z0-9])", r"\1 ", out)
    out = re.sub(r"(?<=[A-Za-z0-9])((?i:age|caste|occ))(?=[A-Z0-9:])", r" \1 ", out)

    out = repair_spacing_artifacts(out)

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
    """(start, end) of the sentence containing ``pos``."""
    start, end = max(0, pos - max_len), min(len(text), pos + max_len)
    for e in _sentence_ends(text[start:end]):
        if start + e <= pos:
            continue
        end = start + e
        break
    for e in _sentence_ends(text[max(0, pos - max_len) : pos]):
        start = max(0, pos - max_len) + e
    return start, end


def join_narrative_lines(lines) -> str:
    """Visual lines of a narrative → readable paragraphs, words unchanged."""
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in lines]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    widths = sorted(len(ln) for ln in lines)
    full = widths[(len(widths) * 3) // 4]
    out = ""
    for i, ln in enumerate(lines):
        if out:
            prev = lines[i - 1]
            if len(prev) < 0.8 * full and re.search(r"[.:;!?)]$", prev):
                out += "\n\n"
            elif len(prev) < 0.5 * full:
                out += "\n"
            elif prev.endswith("-") and ln[:1].islower():
                pass
            else:
                out += " "
        out += ln
    return out


def compact(text: str) -> str:
    """Lowercase letters/digits only — matches glued and spaced variants alike."""
    return re.sub(r"[^a-z0-9]", "", strip_accents(text or "").lower())


def clean_value(text: str) -> str:
    """Collapse whitespace and trim list/sentence punctuation around a value."""
    v = re.sub(r"\s+", " ", (text or "").replace("\u00a0", " ")).strip()
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
    if re.search(r"\bsheeted\b", low) and "date" not in low and "no" not in low:
        return True
    if re.fullmatch(r"(?:dist\.?\s*)?[a-z.]+\s+p\.?s\.?", low):
        return True
    return False
