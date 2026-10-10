"""Stage: parse acts & sections into structured, role-tagged provisions.

Grammar (no per-document patterns):
    [u/s | sec. | section(s)] SECTION_LIST [of [the]] ACT
    ACT [,] (u/s | sec.) SECTION_LIST
    (u/s | section) SECTION_LIST            -- act taken from the clause
where SECTION_LIST is ``8(c), 20(b)(ii)(A)``, ``302, 34``, ``8(C) r/w 22``,
``182/211`` ... Each provision is tagged with a role from its sentence:
charged / registered / altered_from / procedural / reference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from app.schemas.charge_sheet import SourceConflict
from app.services.text_normalize import sentence_bounds, soften_pdf_text

_SEC = r"\d{1,4}(?:-?[A-Z](?![A-Za-z]))?(?:\s*\(\s*[0-9A-Za-z]{1,4}\s*\))*"
_READ_WITH = r"(?:r\s*/\s*w|read\s+with|r\.\s*w\.?)"
_SEC_ITEM = rf"{_SEC}(?:\s*{_READ_WITH}\s*{_SEC})?"
_SEC_LIST = rf"{_SEC_ITEM}(?:\s*(?:,|&|and|/)\s*{_SEC_ITEM})*"
_TRIGGER = r"(?:u\s*/\s*a?s(?:ec)?\.?|under\s+sections?|sections?|secs?\.?|sec\b)"

_ACT_ALTERNATIVES: List[Tuple[str, str]] = [
    ("IPC", r"I\.?\s*P\.?\s*C\.?(?![A-Za-z])|Indian\s+Penal\s+Code"),
    ("BNSS", r"B\.?\s*N\.?\s*S\.?\s*S\.?(?![A-Za-z])|Bharatiya\s+Nagarik\s+Suraksha\s+Sanhita"),
    ("BNS", r"B\.?\s*N\.?\s*S\.?(?![A-Za-z])|Bharatiya\s+Nyaya\s+Sanhita"),
    ("CrPC", r"Cr\.?\s*P\.?\s*C\.?(?![A-Za-z])|Code\s+of\s+Criminal\s+Procedure"),
    ("NDPS Act", r"N\.?\s*D\.?\s*P\.?\s*S\.?(?:\s*Act)?(?![A-Za-z])"),
    (
        "STATE_ACT",
        r"(?:A\.?\s*P\.?|T\.?\s*S\.?|Telangana(?:\s+State)?|Andhra\s+Pradesh|Karnataka|Maharashtra"
        r"|Tamil\s+Nadu|Kerala|Bihar|Gujarat|Punjab)\s+(?:State\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+Act",
    ),
    ("GENERIC", r"(?:[A-Z(][A-Za-z.&/()\-]*\s+(?:(?:of|and|the|for|to|on)\s+)?){1,5}Act(?![A-Za-z])"),
]
_ACT = "(?:" + "|".join(f"(?:{p})" for _, p in _ACT_ALTERNATIVES) + r")(?:\s*[-,]?\s*(?:of\s+)?(?:19|20)\d{2})?"

_PATTERN = re.compile(
    rf"(?:(?P<trig_a>{_TRIGGER})\s*)?(?P<secs_a>{_SEC_LIST})\s*,?\s*(?:of\s+(?:the\s+)?)?(?P<act_a>{_ACT})"
    rf"|(?P<act_c>{_ACT})\s*[,.]?\s*{_TRIGGER}\s*(?P<secs_c>{_SEC_LIST})"
    rf"|(?P<trig_b>{_TRIGGER})\s*(?P<secs_b>{_SEC_LIST})",
    re.I,
)
_PROCEDURAL_ACTS = {"CrPC", "BNSS"}
_CHARGE_WORDS = re.compile(
    r"(?i)punishable|made\s*out|committed\s*(?:the\s*)?offen|pray|punish|charge\s*sheet(?:ed)?\s*(?:u/s|under)"
    r"|charged|guilty|offences?\s+(?:u/s|under)|hence\s+the\s+charge"
)
_AFTER_CHARGE = re.compile(r"(?i)^\W{0,3}(?:is|are|were|was)?\s*(?:made\s*out|added|invoked|inserted|incorporated)")


@dataclass
class Provision:
    act: Optional[str]
    section: str
    role: str
    page: Optional[int]
    source_text: str
    zone: str


@dataclass
class LegalEntry:
    act: Optional[str]
    section: str
    role: str
    pages: List[int] = field(default_factory=list)
    source_text: str = ""


def normalize_act(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    a = re.sub(r"\s+", " ", raw).strip(" ,.-")
    a = re.sub(r"[\s,\-]*(?:of\s+)?(?:19|20)\d{2}$", "", a).strip(" ,.-")
    compact_a = re.sub(r"[^A-Za-z]", "", a).upper()
    for name, pattern in _ACT_ALTERNATIVES[:5]:
        if re.fullmatch(pattern, a, re.I):
            return name
    if compact_a in {"IPC", "BNS", "BNSS", "CRPC"}:
        return {"CRPC": "CrPC"}.get(compact_a, compact_a)
    if compact_a.startswith("NDPS"):
        return "NDPS Act"
    m = re.match(r"(?i)^(A\.?\s*P\.?|T\.?\s*S\.?)\s+(.*)$", a)
    if m:
        state = "A.P." if m.group(1).upper().startswith("A") else "T.S."
        return f"{state} {_title(m.group(2))}"
    return _title(a)


def _title(text: str) -> str:
    words = []
    for w in text.split():
        words.append(w if (w.isupper() or "." in w) else (w.lower() if w.lower() in {"of", "and", "the", "for", "to", "on"} else w[:1].upper() + w[1:]))
    return " ".join(words)


def act_family(act: Optional[str]) -> Optional[str]:
    """'A.P. Excise Act' and 'T.S. Excise Act' share the family 'Excise Act'."""
    if not act:
        return None
    return re.sub(
        r"(?i)^(?:A\.P\.|T\.S\.|Telangana(?:\s+State)?|Andhra\s+Pradesh|Karnataka|Maharashtra|Tamil\s+Nadu|Kerala|Bihar|Gujarat|Punjab)\s+",
        "",
        act,
    ).strip()


def _split_sections(secs: str) -> List[str]:
    items = re.split(r"\s*(?:,|&|\band\b|/(?!\s*w))\s*", secs.strip(), flags=re.I)
    out = []
    for item in items:
        item = re.sub(r"\s+", " ", item).strip(" ,.")
        item = re.sub(r"\s*\(\s*", "(", item)
        item = re.sub(r"\s*\)\s*", ")", item)
        item = re.sub(rf"(?i)\s*{_READ_WITH}\s*", " r/w ", item).strip()
        if item:
            out.append(item)
    return out


def _plausible(section: str) -> bool:
    head = re.match(r"\d+", section)
    if not head:
        return False
    n = int(head.group(0))
    if 1900 <= n <= 2100 and "(" not in section:
        return False  # a year, not a section
    return 0 < n <= 600 or "(" in section


def section_key(section: str) -> str:
    return re.sub(r"\s+", "", section.lower())


def _clause_before(text: str, start: int) -> str:
    """The current sentence up to ``start`` (abbreviations like 'Cr. No.' don't end it)."""
    s, _ = sentence_bounds(text, start, max_len=220)
    return text[s:start]


def _role(text: str, m: re.Match, act: Optional[str], zone: str, default_role: Optional[str]) -> str:
    if act in _PROCEDURAL_ACTS:
        return "procedural"
    if default_role:
        return default_role
    before = _clause_before(text, m.start())
    after = text[m.end() : m.end() + 60]
    if re.search(r"(?i)\bfalse\b", before):
        return "reference"
    alter = re.search(r"(?i)alter|amend|chang", before)
    if alter:
        return "charged" if re.search(r"(?i)\bto\b", before[alter.end():]) else "altered_from"
    if _AFTER_CHARGE.search(after) or _CHARGE_WORDS.search(before):
        return "charged"
    if re.search(r"(?i)regist", before):
        return "registered"
    return "reference"


def parse_provisions(
    text: str,
    *,
    zone: str,
    page: Optional[int] = None,
    page_at: Optional[Callable[[int], Optional[int]]] = None,
    default_role: Optional[str] = None,
) -> List[Provision]:
    """All provisions cited in ``text``. ``default_role`` forces the role (form Acts field)."""
    soft = soften_pdf_text(text or "")
    # "u188 IPC" — a lone 'u' glued to the number is a truncated u/s
    soft = re.sub(r"(?i)\bu\s+(?=\d)", "u/s ", soft)
    found: List[Provision] = []
    last_act: Optional[str] = None
    for m in _PATTERN.finditer(soft):
        if m.group("secs_a"):
            secs, act_raw = m.group("secs_a"), m.group("act_a")
            if not m.group("trig_a") and not re.search(r"\d", secs):
                continue
        elif m.group("secs_c"):
            secs, act_raw = m.group("secs_c"), m.group("act_c")
        else:
            secs, act_raw = m.group("secs_b"), None
        act = normalize_act(act_raw) if act_raw else None
        if act is None:
            # Trigger-only citation: borrow the act named just before in the clause
            clause = _clause_before(soft, m.start())
            acts_before = list(re.finditer(_ACT, clause))
            act = normalize_act(acts_before[-1].group(0)) if acts_before else last_act
            if act is None:
                continue
        last_act = act
        role = _role(soft, m, act, zone, default_role if act not in _PROCEDURAL_ACTS else None)
        src = re.sub(r"\s+", " ", soft[max(0, m.start() - 40) : m.end() + 20]).strip()
        pg = page_at(m.start()) if page_at else page
        for sec in _split_sections(secs):
            if _plausible(sec):
                found.append(Provision(act, sec, role, pg, src[:200], zone))
    return found


def consolidate(provisions: List[Provision]) -> Tuple[List[LegalEntry], List[LegalEntry], List[SourceConflict]]:
    """(applicable charges, other references, act-name conflicts).

    Applicable charges are the 'charged' provisions (form Acts field, charging
    statements, alteration targets). Only when the document has none do the
    registration sections stand in, still tagged 'registered'.
    """
    groups: Dict[Tuple[str, str], List[Provision]] = {}
    for p in provisions:
        groups.setdefault(((p.act or "").lower(), section_key(p.section)), []).append(p)
    applicable = "charged" if any(p.role == "charged" for p in provisions) else "registered"
    other_priority = ["altered_from", "registered", "reference", "procedural"]

    charged: List[LegalEntry] = []
    others: List[LegalEntry] = []
    for group in groups.values():
        roles = {p.role for p in group}
        pages = sorted({p.page for p in group if p.page})
        # Display the form-zone wording when available
        rep = next((p for p in group if p.zone == "form"), group[0])
        if applicable in roles:
            src = next((p for p in group if p.role == applicable), rep)
            charged.append(LegalEntry(rep.act, rep.section, applicable, pages, src.source_text))
        else:
            role = next(r for r in other_priority + sorted(roles) if r in roles)
            others.append(LegalEntry(rep.act, rep.section, role, pages, rep.source_text))

    conflicts: List[SourceConflict] = []
    by_section: Dict[Tuple[str, str], Set[str]] = {}
    pages_by_section: Dict[Tuple[str, str], Set[int]] = {}
    for p in provisions:
        if p.role in {"procedural", "reference"} or not p.act:
            continue
        fam = act_family(p.act) or ""
        k = (fam.lower(), section_key(p.section))
        by_section.setdefault(k, set()).add(p.act)
        if p.page:
            pages_by_section.setdefault(k, set()).add(p.page)
    for (fam, sec), acts in by_section.items():
        if len(acts) > 1:
            conflicts.append(
                SourceConflict(
                    field_name=f"legal.act[{sec}]",
                    values=sorted(acts),
                    source_pages=sorted(pages_by_section.get((fam, sec), set())),
                    evidence=f"Section {sec} is cited under different act names in the source",
                    resolution="unresolved",
                )
            )
    return charged, others, conflicts
