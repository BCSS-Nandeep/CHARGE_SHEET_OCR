"""Split report markdown into the 32 standard sections.

Headings are matched by title similarity first and by number second, so a
report that renumbers or reorders slightly still lines up with the standard
template. Sections that are not in the report are reported as not found; they
are not guessed.
"""

from __future__ import annotations

import difflib
import re
from typing import Dict, List, Optional, Tuple

from app.ir_standard.schema import SECTIONS, SectionSpec

# "3. TITLE", "F. TITLE" (lettered annexure items) or a bare "TITLE"; the printed marker is optional
_HEADING = re.compile(r"^\s*(?:(?:(\d{1,2})\s*[.)]\s*)|(?:[A-Za-z]\s*[.)]\s+))?(.{3,})$")
_MAX_UNNUMBERED_LEN = 90  # a heading is short; longer lines are narrative or values
_ANNEX3 = re.compile(r"annexure\W*(iii|3)\b", re.IGNORECASE)
_TITLE_OK = 0.62  # title alone is enough
_TITLE_WITH_NUMBER = 0.38  # number must agree as well


def _clean(line: str) -> str:
    line = re.sub(r"^[\s#>*_|☐☑]+|[\s*_|]+$", "", line.replace("**", "")).strip()
    return re.sub(r"\s+", " ", line)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _score(heading_title: str, number: Optional[int], spec: SectionSpec) -> float:
    h, t = _norm(heading_title), _norm(spec.title)
    ratio = difflib.SequenceMatcher(None, h, t).ratio()
    # containment counts only when the line is about as long as the title (not a narrative line)
    if (t in h or h in t) and len(h) <= len(t) * 1.6 + 8 and len(h) >= len(t) * 0.5:
        ratio = max(ratio, 0.8)
    if h.startswith(t) and len(t) >= 8:  # "DOPAMS Links of the accused arrested ..." still names section "DOPAMS Links"
        ratio = max(ratio, 0.8)
    if ratio >= _TITLE_OK:
        return ratio
    if number == spec.number and ratio >= _TITLE_WITH_NUMBER:
        return ratio + 0.2
    return 0.0


def split_sections(markdown: str) -> Tuple[Dict[str, str], List[str]]:
    """Return ({section_key: text}, [keys of sections not found])."""
    lines = markdown.splitlines()
    candidates: List[Tuple[int, Optional[int], str]] = []  # (line index, printed number or None, title)
    for i, raw in enumerate(lines):
        if raw.lstrip().startswith("|") and raw.count("|") > 2:
            continue  # table row, not a heading
        cleaned = _clean(raw)
        m = _HEADING.match(cleaned)
        if not m:
            continue
        number = int(m.group(1)) if m.group(1) else None
        if number is None and len(cleaned) > _MAX_UNNUMBERED_LEN:
            continue
        candidates.append((i, number, m.group(2)))

    chosen: Dict[int, int] = {}  # section number -> line index
    last = -1
    for spec in SECTIONS:
        if spec.number == 0:
            continue
        best = None
        for idx, num, title in candidates:
            if idx <= last:
                continue
            s = _score(title, num, spec)
            if s and (best is None or s > best[0] + 1e-9):
                best = (s, idx)
                if s >= 0.95:
                    break
        if best:
            chosen[spec.number] = best[1]
            last = best[1]

    ends: Dict[int, int] = {}
    ordered = sorted(chosen.items(), key=lambda kv: kv[1])
    annex3 = next((i for i, ln in enumerate(lines) if _ANNEX3.search(_clean(ln))), None)
    for pos, (num, start) in enumerate(ordered):
        stop = ordered[pos + 1][1] if pos + 1 < len(ordered) else len(lines)
        if annex3 is not None and start < annex3 < stop:
            stop = annex3
        ends[num] = stop

    out: Dict[str, str] = {}
    for spec in SECTIONS:
        if spec.number and spec.number in chosen:
            out[spec.key] = "\n".join(lines[chosen[spec.number]: ends[spec.number]]).strip()

    first = ordered[0][1] if ordered else len(lines)
    narrative = lines[:first]
    if annex3 is not None and ordered and annex3 >= ends[ordered[-1][0]]:
        narrative = narrative + lines[annex3:]
    elif not ordered:
        narrative = lines
    out["s00_narrative"] = "\n".join(narrative).strip()

    missing = [s.key for s in SECTIONS if s.number and s.key not in out]
    return out, missing
