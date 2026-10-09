"""Stage: structured accused / witness / person entity extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

_WS = re.compile(r"\s+")


@dataclass
class PersonEntity:
    serial_no: Optional[str] = None
    name: Optional[str] = None
    parent_or_spouse: Optional[str] = None
    age: Optional[str] = None
    caste: Optional[str] = None
    occupation: Optional[str] = None
    address: Optional[str] = None
    identification: Optional[str] = None  # Aadhaar etc.
    contact: Optional[str] = None
    role: Optional[str] = None
    raw: str = ""
    source_page: Optional[int] = None
    name_variants: List[str] = field(default_factory=list)
    age_mentions: List[str] = field(default_factory=list)


def _clean(text: str) -> str:
    return _WS.sub(" ", (text or "").replace("\n", " ")).strip(" ,.;")


def parse_person_blob(blob: str, page: Optional[int] = None, serial: Optional[str] = None) -> PersonEntity:
    text = _clean(blob)
    ent = PersonEntity(raw=text[:800], source_page=page, serial_no=serial)

    # Age
    for m in re.finditer(r"age\s*[:\-]?\s*(\d{1,3})\s*(?:yrs?|years?)?", text, re.I):
        ent.age_mentions.append(m.group(1))
    if ent.age_mentions:
        ent.age = ent.age_mentions[0]

    # Parentage
    pm = re.search(r"\b(s/o|d/o|w/o|so|do|wo)\s*[:\-]?\s*([^,]+?)(?:,|age\b|caste\b|occ\b|r/o\b|$)", text, re.I)
    if pm:
        ent.parent_or_spouse = f"{pm.group(1)} {_clean(pm.group(2))}"

    # Aadhaar / Adhar
    am = re.search(r"(?:adhar|aadhaar|aadhar)\s*(?:no\.?)?\s*[:\-]?\s*([0-9]{4}\s?[0-9]{4}\s?[0-9]{4})", text, re.I)
    if am:
        ent.identification = re.sub(r"\s+", "", am.group(1))

    # Contact
    cm = re.search(r"(?:contact|mobile|phone)\s*[:\-]?\s*([0-9]{10})", text, re.I)
    if not cm:
        cm = re.search(r"\b([6-9][0-9]{9})\b", text)
    if cm:
        ent.contact = cm.group(1)

    # Caste / Occ / Address
    caste_m = re.search(r"caste\s*[:\-]?\s*([^,]+)", text, re.I)
    if caste_m:
        ent.caste = _clean(caste_m.group(1))
    occ_m = re.search(r"(?:occ|occupation)\s*[:\-]?\s*([^,]+)", text, re.I)
    if occ_m:
        ent.occupation = _clean(occ_m.group(1))
    addr_m = re.search(
        r"(?:r/o\.?|resident(?:\s+of)?)\s*[:\-]?\s*(.+?)(?="
        r"(?:adhar|aadhaar|contact|issued|apprehend|arrest|confess|voluntar|"
        r"investigation|brief\s+facts|during\s+the|hence|therefore|$))",
        text,
        re.I,
    )
    if addr_m:
        addr = _clean(addr_m.group(1))
        # Hard cap — addresses are short; narrative bleed is invalid
        if len(addr) > 180:
            addr = addr[:180].rsplit(" ", 1)[0]
        ent.address = addr

    # Name: text before s/o / age
    name_part = re.split(r"\b(?:s/o|d/o|w/o|so|do|wo|age)\b", text, maxsplit=1, flags=re.I)[0]
    name_part = re.sub(
        r"^(?:name\s+of\s+accused.*?sheeted\.?|person\(s\)\s*charge\s*sheeted\.?|"
        r"charge\s*sheeted\.?|accused\s*:?)\s*",
        "",
        name_part,
        flags=re.I,
    )
    # Drop leading serial markers but keep honorific with name
    name_part = re.sub(r"^(?:LW\s*[-–]?\s*\d+|A\s*[-–]?\s*\d+\s*[\.\):]?|0?\d{1,2})\s*", "", name_part, flags=re.I)
    name_part = _clean(name_part)
    # Drop trailing punctuation noise
    name_part = re.sub(r"^(?:and\s+)?", "", name_part, flags=re.I)
    if name_part and not re.match(r"^(?:-|mentioned|nil|not\b)", name_part, re.I):
        # Avoid absorbing form numbers only
        if not re.match(r"^\d+$", name_part) and len(name_part) > 2:
            ent.name = name_part[:120]
            ent.name_variants.append(ent.name)

    # Role hints for witnesses
    role_m = re.search(
        r"(eye\s*witness|panch|land\s*owner|i\.?o\.?|complainant|assisted\s+the\s*i\.?o)",
        text,
        re.I,
    )
    if role_m:
        ent.role = _clean(role_m.group(1))

    return ent


def extract_accused_from_block(block: str, page: int = 1) -> List[PersonEntity]:
    if not block:
        return []
    # Cut at next major section if present inside block
    cut = re.search(
        r"(?:name\s+of\s+accused\s+persons?\(s\)\s*not|list\s+of\s+witnesses|"
        r"final\s+report\s+is\s+false|brief\s+facts|witnesses\b)",
        block,
        re.I,
    )
    text = block[: cut.start()] if cut else block
    # Remove heading
    text = re.sub(
        r"^.*?(?:charge\s*sheeted\.?|particulars\s+of\s+(?:the\s+)?accused:?|list\s+of\s+accused:?|accused:?)\s*",
        "",
        text,
        count=1,
        flags=re.I | re.S,
    )
    text = _clean(text)
    if not text or text in {"-", "—", "nil", "."}:
        return []

    # Split on A1/A2/A3 or numbered persons (works with softened text)
    people: List[PersonEntity] = []
    chunks = re.split(
        r"(?=(?:\bA\s*[-–]?\s*\d+\s*[\.\):]|\bA\d+\s*[\.\):]|\b\d{1,2}\s*[\.\)]\s*[A-Za-z]))",
        text,
        flags=re.I,
    )
    chunks = [c.strip() for c in chunks if c.strip() and len(c.strip()) > 8]
    if not chunks:
        chunks = [text]

    for i, chunk in enumerate(chunks):
        # Skip pure boilerplate / headings / whole-page bleed
        low = chunk.lower()
        if re.match(r"^(?:name\s+of\s+accused|person\(s\)|particulars\s+of)", chunk, re.I) and "s/o" not in low and "age" not in low:
            continue
        if "police station" in low and "fir" in low and "s/o" not in low:
            continue
        sno = None
        sm = re.match(r"(?:A\s*[-–]?\s*(\d+)|(\d{1,2})\s*[\.\)])", chunk, re.I)
        if sm:
            sno = sm.group(1) or sm.group(2)
        ent = parse_person_blob(chunk, page=page, serial=sno or str(i + 1))
        if ent.name and not re.match(r"^(?:particulars|form of|district|charge)", ent.name, re.I):
            people.append(ent)
    # Deduplicate by normalized name
    seen = set()
    unique: List[PersonEntity] = []
    for p in people:
        key = re.sub(r"[^a-z]", "", (p.name or "").lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def extract_witnesses_from_block(block: str, page: int = 1) -> List[PersonEntity]:
    if not block:
        return []
    cut = re.search(
        r"(?:final\s+report\s+is\s+false|brief\s+facts|result\s+of\s+laboratory|"
        r"15\s+final|in\s+the\s+court\s+of|hon'?ble\s+sir|filed\s+charge\s*sheet)",
        block,
        re.I,
    )
    text = block[: cut.start()] if cut else block
    # Bound witness list size — ignore narrative dump
    if len(text) > 4000:
        text = text[:4000]
    text = re.sub(r"^.*?examined\.?\s*", "", text, count=1, flags=re.I | re.S)
    text = re.sub(r"mentioned\s+below\.?\s*", "", text, flags=re.I)

    # Split only on real witness serials (number + honorific/name), not ages like "34 yrs"
    parts = re.split(
        r"(?=(?:\b\d{1,3}\s+(?:Sri\.?|Smt\.?|Ms\.?|Mr\.?|Dr\.?)|"
        r"\bLW\s*[-–]?\s*\d+\b))",
        text,
        flags=re.I,
    )
    people: List[PersonEntity] = []
    for part in parts:
        part = part.strip()
        if len(part) < 10:
            continue
        sm = re.match(r"(?:LW\s*[-–]?\s*(\d+)|(\d{1,3}))\s+(?:Sri\.?|Smt\.?|Ms\.?|Mr\.?|Dr\.?)", part, re.I)
        sno = None
        if sm:
            sno = sm.group(1) or sm.group(2)
            if sno and len(sno) == 3 and sno.startswith("30"):
                sno = sno[1:]
            elif sno and len(sno) == 3 and sno[0] == "3":
                sno = sno[1:]
        cleaned = re.sub(
            r"^(?:LW\s*[-–]?\s*\d+|\d{1,3})\s+",
            "",
            part,
            count=1,
            flags=re.I,
        )
        ent = parse_person_blob(cleaned, page=page, serial=sno)
        if not ent.name:
            continue
        if re.match(r"^(?:list|mentioned|witness|yrs|caste|occ|age)\b", ent.name, re.I):
            continue
        if re.search(r"(?i)\b(?:caste|occ|yrs)\b", ent.name) and len(ent.name) > 40:
            continue
        people.append(ent)

    seen = set()
    unique: List[PersonEntity] = []
    for p in people:
        key = re.sub(r"[^a-z]", "", (p.name or "").lower())[:24]
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def find_name_age_mentions(full_text: str, base_name: str) -> PersonEntity:
    """Collect alternate spellings / ages for an accused across the document.

    General heuristic: share a surname/core token and a nearby age mention.
    No sample-specific names.
    """
    ent = PersonEntity(name=base_name, name_variants=[base_name] if base_name else [])
    tokens = [t for t in re.split(r"\s+", base_name or "") if len(t) > 3]
    if not tokens:
        return ent
    core = tokens[-1]
    given = tokens[0] if tokens else ""
    given_prefix = given[:4].lower() if given else ""

    pattern = (
        rf"((?:[A-Za-z]{{3,15}})\s+){{0,2}}"
        rf"((?:[A-Za-z]+\s+){{0,3}}{re.escape(core)})"
        rf".{{0,100}}?age\s*[:\-]?\s*(\d{{1,3}})"
    )
    for m in re.finditer(pattern, full_text, re.I | re.S):
        variant = _clean(m.group(0).split("age")[0])
        # Trim leading junk words
        variant = re.sub(
            r"^(?:the\s+)?(?:accused\s+person|accused|person)\s+",
            "",
            variant,
            flags=re.I,
        ).strip(" ,.-")
        age = m.group(3)
        if not variant or len(variant.split()) < 2:
            continue
        if re.match(r"^(?:charge|person|name|accused|the|apprehend)\b", variant, re.I):
            continue
        from difflib import SequenceMatcher

        first_tok = variant.split()[0]
        same_family = core.lower() in variant.lower()
        similar_given = bool(given_prefix) and first_tok.lower().startswith(given_prefix[:3])
        # Spelling drift on the given name (e.g. leading-letter variants)
        drift = (
            len(given) >= 4
            and len(first_tok) >= 4
            and SequenceMatcher(None, given.lower(), first_tok.lower()).ratio() >= 0.7
        )
        if same_family and (similar_given or drift or given.lower() in variant.lower()):
            if variant.lower() not in {v.lower() for v in ent.name_variants}:
                # Keep short person-name shaped variants only (no parentage/age tails)
                variant = re.split(r"\b(?:s/o|d/o|w/o|age)\b", variant, maxsplit=1, flags=re.I)[0].strip(" ,.-")
                if (
                    4 <= len(variant) <= 60
                    and len(variant.split()) >= 2
                    and not re.search(r"\b(?:confess|apprehend|investigation)\b", variant, re.I)
                ):
                    ent.name_variants.append(variant)
        if age and age not in ent.age_mentions:
            ent.age_mentions.append(age)

    if ent.age_mentions:
        ent.age = ent.age_mentions[0]
    return ent
