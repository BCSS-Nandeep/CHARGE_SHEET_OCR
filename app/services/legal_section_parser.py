"""Stage: parse acts & sections into deduplicated structured records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple


@dataclass
class ParsedSection:
    act: Optional[str]
    section: str
    source_page: int
    source_text: str


_ACTS = (
    r"I\.?\s*P\.?\s*C\.?|IPC|BNS|Cr\.?\s*P\.?\s*C\.?|BNSS|NDPS|"
    r"(?:A\.?\s*P\.?|T\.?\s*S\.?|Telangana|Andhra)\s+Excise\s+Act|"
    r"Arms\s+Act|POCSO|IT\s+Act"
)

_SECTION_CHUNK = re.compile(
    rf"""
    (?:
        (?:u/?s|under\s+section|sections?|sec\.?)\s*
    )?
    (?P<section>
        \d+[A-Za-z]?(?:\([A-Za-z0-9]+\))?(?:\s*r/?w\s*\d+[A-Za-z]?(?:\([A-Za-z0-9]+\))?)?
        (?:\s*,\s*\d+[A-Za-z]?(?:\([A-Za-z0-9]+\))?)*
    )
    \s*
    (?P<act>{_ACTS})?
    """,
    re.I | re.X,
)


def _norm_act(act: Optional[str]) -> Optional[str]:
    if not act:
        return None
    a = re.sub(r"\s+", " ", act).strip(" ,.")
    a = re.sub(r"I\.\s*P\.\s*C\.?", "IPC", a, flags=re.I)
    a = re.sub(r"A\.?P\.?\s+Excise\s+Act", "A.P. Excise Act", a, flags=re.I)
    a = re.sub(r"T\.?S\.?\s+Excise\s+Act", "T.S. Excise Act", a, flags=re.I)
    if re.search(r"NDPS", a, re.I):
        return "NDPS Act"
    if a.upper() == "IPC":
        return "IPC"
    return a


def _norm_section(sec: str) -> str:
    s = re.sub(r"\s+", " ", sec).strip(" ,./")
    s = re.sub(r"\s*,\s*", ", ", s)
    return s


def _is_plausible_section(sec: str) -> bool:
    s = sec.strip()
    if not s or len(s) > 40:
        return False
    if s.lower() in {"s", "of", "law", "to", "the"}:
        return False
    # Reject bare form numbers / times / phone-like / years alone
    if re.fullmatch(r"\d{1,2}", s):
        return False
    if re.fullmatch(r"(19|20)\d{2}", s):
        return False
    if re.fullmatch(r"\d{5,}", s):
        return False
    if re.search(r"(?:yrs?|hours?|hrs?|lts?)$", s, re.I):
        return False
    if not re.search(r"\d", s):
        return False
    return True


def parse_legal_sections(text: str, page: int, *, focused_only: bool = False) -> List[ParsedSection]:
    if not text:
        return []
    found: List[ParsedSection] = []
    seen: Set[Tuple[Optional[str], str]] = set()

    # Prefer the dedicated Act/Sections form region (stop before next form column)
    focus = text
    m = re.search(
        r"(?:0?4\s+act\.?|acts?\s+and\s+sections|sections?\s*:)(.{0,350}?)(?=(?:\n\s*0?5\s+)|$)",
        text,
        re.I | re.S,
    )
    if m:
        focus = m.group(0)
    elif focused_only:
        # Caller asked for focused parse but no heading — use short prefix only
        focus = text[:350]

    explicit = [
        re.compile(r"(?P<section>188)\s*(?P<act>IPC)", re.I),
        re.compile(
            r"(?P<section>34\s*\(\s*[aA]\s*\))\s*(?P<act>(?:A\.?\s*P\.?|T\.?\s*S\.?)\s*Excise\s*Act)",
            re.I,
        ),
        re.compile(
            r"(?:u/?s\s*)?(?P<section>8\s*\(\s*[cC]\s*\)\s*r/?w\s*22)\s*(?:of\s+)?(?P<act>NDPS\s*Act(?:\s*[-–]?\s*1985)?)",
            re.I,
        ),
        # U/Sec. 8(c), 20(b)(ii)(A) of NDPS Act  (common NDPS charge-sheet form)
        re.compile(
            r"(?:u/?s(?:ec)?\.?\s*)?(?P<section>8\s*\(\s*[cC]\s*\)\s*,\s*20\s*\(\s*[bB]\s*\)\s*\(\s*ii\s*\)\s*\(\s*[A-Za-z]\s*\))"
            r"\s*(?:of\s+)?(?P<act>NDPS\s*Act)",
            re.I,
        ),
        re.compile(
            r"(?:u/?s(?:ec)?\.?\s*)?(?P<section>20\s*\(\s*[bB]\s*\)\s*\(\s*ii\s*\)\s*\(\s*[A-Za-z]\s*\))"
            r"\s*(?:of\s+)?(?P<act>NDPS\s*Act)",
            re.I,
        ),
        re.compile(
            r"(?:u/?s(?:ec)?\.?\s*)?(?P<section>8\s*\(\s*[cC]\s*\))\s*(?:of\s+)?(?P<act>NDPS\s*Act)",
            re.I,
        ),
    ]
    for rx in explicit:
        for em in rx.finditer(focus):
            act = _norm_act(em.group("act"))
            sec = _norm_section(em.group("section"))
            key = ((act or "").lower(), sec.lower())
            if key in seen or not _is_plausible_section(sec):
                continue
            seen.add(key)
            found.append(
                ParsedSection(
                    act=act,
                    section=sec,
                    source_page=page,
                    source_text=em.group(0)[:200],
                )
            )

    # Generic fallback only inside focused act region
    for gm in _SECTION_CHUNK.finditer(focus):
        sec = _norm_section(gm.group("section") or "")
        act = _norm_act(gm.group("act"))
        if not _is_plausible_section(sec):
            continue
        # Require an act for generic matches to avoid form-number noise
        if not act:
            continue
        key = ((act or "").lower(), sec.lower())
        if key in seen:
            continue
        seen.add(key)
        found.append(
            ParsedSection(
                act=act,
                section=sec,
                source_page=page,
                source_text=gm.group(0)[:200],
            )
        )
    return found


def merge_section_lists(*lists: List[ParsedSection]) -> List[ParsedSection]:
    seen: Set[Tuple[Optional[str], str]] = set()
    out: List[ParsedSection] = []
    for lst in lists:
        for item in lst:
            key = (item.act, item.section.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out
