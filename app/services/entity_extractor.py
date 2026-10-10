"""Stage: structured accused / witness / officer entity extraction.

A person entry in a charge sheet is a run of attribute markers
("Name S/o Father, Age: 33, Caste: X, Occ: Y, R/o Address, Ph: 9xxxxxxxxx").
Each attribute value ends where the next marker (or a phone number / ID)
begins, so a narrative or role column can never be absorbed into an address.
All markers are matched on word boundaries ("age" never matches "PAGE").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

from app.services.text_normalize import clean_value, compact, soften_pdf_text

_L = r"(?<![A-Za-z])"
_R = r"(?![A-Za-z])"

_PARENT = re.compile(
    rf"(?i){_L}(?:[sdwch]\s*/\s*o{_R}|[sdwch]\s*/\s*\.|son\s+of{_R}|daughter\s+of{_R}|wife\s+of{_R}|husband\s+of{_R})\.?"
    rf"|{_L}(?:so|do|wo){_R}(?=\s+[A-Z])"
)
_AGE = re.compile(rf"(?i){_L}(?:age|aged){_R}\s*[:;.\-]?\s*(\d{{1,3}})(?!\d)\s*(?:years?|yrs?)?\.?")
_AGE_BARE = re.compile(r"(?i)(?<![\d/])(\d{1,3})\s*(?:years?|yrs)(?![A-Za-z])")
_CASTE = re.compile(rf"(?i){_L}caste{_R}\s*[:;.\-]?\s*")
_OCC = re.compile(rf"(?i){_L}occ(?:upation)?{_R}\.?\s*[:;.\-]?\s*")
_ADDR = re.compile(rf"(?i){_L}(?:r\s*/\s*o|resident\s+of|residing\s+at){_R}\.?\s*[:;.\-]?\s*")
_PHONE_LABEL = re.compile(
    rf"(?i){_L}(?:ph(?:one)?|cell|mobile|mob|contact)\.?\s*(?:no\.?|number)?{_R}\s*[:;.\-]?\s*"
)
_AADHAAR_LABEL = re.compile(
    rf"(?i){_L}(?:aadhaa?r|adhaa?r|uid){_R}\s*(?:card)?\s*(?:no\.?|number)?\s*[:;.\-]?\s*"
)
_PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
_ID12 = re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)")
_PAREN = re.compile(r"\(([^()]{2,240})\)")

_HONORIFIC = re.compile(
    r"^(?:sri|shri|smt|srimathi|shrimati|kum|kumari|mr|mrs|ms|miss|dr)\b\.?,?\s*", re.I
)
_SERIAL_PREFIX = re.compile(
    r"^(?:A\s*[-–]?\s*\d{1,2}\s*[.):\-]?|(?:LW|PW|CW|DW)\s*[-–.]?\s*\d{1,3}\s*[.):\-]?|\(?\d{1,2}\s*[.)])\s*",
    re.I,
)
# Designations end a name and become the occupation. Acronyms are case-sensitive.
_DESIGNATION = re.compile(
    r"(?<![A-Za-z])(?:"
    r"SIP|S\.?I\.?(?=\s*of\b|\s|,|$)|A\.?S\.?I\.?|SHO|SDPO|D\.?S\.?P\.?|Dy\.?\s*S\.?P\.?|Addl\.?\s*S\.?P\.?|S\.?P\.?(?=\s|,|$)"
    r"|ACP|DCP|CI|MRO|VRA|IO|I\.O\.?"
    r"|(?i:(?:w?pc|hc|hg)\s*[-–]?\s*\d+)"
    r"|(?i:sub[\s-]*inspector|inspector|head\s+constable|constable|home\s*guard"
    r"|tahs?ildar|tahasildar|revenue\s+inspector|executive\s+magistrate|medical\s+officer"
    r"|police\s+constable|village\s+revenue|panchayat\s+secretary)"
    r")(?![A-Za-z])"
)
_ROLE_WORDS = re.compile(
    r"(?i)(?<![A-Za-z])(?:eye\s*-?\s*witness|panch(?:a|es)?|mediators?|land\s*-?\s*owner|complainant"
    r"|informant|investigating\s+officer|i\.?\s?o\.?|assisted\s+(?:to|the)\s+i\.?\s?o"
    r"|witness\s+to|conducted\s+(?:the\s+)?panchanama|doctor|scribe|filed\s+charge\s*sheet)(?![A-Za-z])"
)
_ARREST_WORDS = re.compile(
    r"(?i)arrest|remand|absconding|abscond|bail|custody|notice|41\s*-?\s*a|surrender|judicial|pt\s*warrant"
)
_ACCUSED_MARK = re.compile(r"(?<![A-Za-z0-9])A\s*[-–]?\s*(\d{1,2})\s*[.):\-]?\s*(?=[A-Z(])")
_ACCUSED_REF = re.compile(r"(?<![A-Za-z0-9])A\s*[-–]?\s*(\d{1,2})(?!\d)")
_NUMBERED_LINE = re.compile(r"(?m)^\s*\(?(\d{1,2})\s*[.)]\s+(?=\S)")

_NAME_STOPWORDS = {
    "a", "an", "the", "as", "namely", "i.e", "i.e.", "ie", "viz", "of", "and", "with", "by", "to",
    "from", "in", "on", "at", "that", "his", "her", "their", "name", "names", "is", "was", "were",
    "are", "accused", "person", "persons", "witness", "witnesses", "panch", "panchas", "punches",
    "mediators", "revealed", "confessed", "one", "two", "namely", "he", "she", "they", "him",
    "apprehend", "apprehended", "arrested", "against", "said", "above", "first", "second", "i.e.,",
}


@dataclass
class PersonEntity:
    serial_no: Optional[str] = None
    name: Optional[str] = None
    alias: Optional[str] = None
    parent_or_spouse: Optional[str] = None
    age: Optional[str] = None
    caste: Optional[str] = None
    occupation: Optional[str] = None
    address: Optional[str] = None
    identification: Optional[str] = None
    contact: Optional[str] = None
    role: Optional[str] = None
    role_note: Optional[str] = None
    arrest_status: Optional[str] = None
    details: Optional[str] = None
    raw: str = ""
    source_page: Optional[int] = None


@dataclass
class PersonMention:
    """A person described elsewhere in the document (narrative), for cross-checks."""

    name: str
    parent: Optional[str]
    age: Optional[str]
    page: Optional[int]
    text: str


def _relation(marker: str) -> str:
    """'S/.', 's/o', 'son of', 'so' → 'S/o' (D/o, W/o, C/o, H/o likewise)."""
    m = marker.strip().lower()
    for prefix, rel in (("son", "S/o"), ("daughter", "D/o"), ("wife", "W/o"), ("husband", "H/o")):
        if m.startswith(prefix):
            return rel
    return f"{m[:1].upper()}/o" if m else "S/o"


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", soften_pdf_text(text or "").replace("\n", " ")).strip()


def clean_name(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Strip serial markers / honorifics; split '@ alias'. Returns (name, alias)."""
    t = clean_value(text)
    for _ in range(3):
        t2 = _SERIAL_PREFIX.sub("", t, count=1)
        t2 = _HONORIFIC.sub("", t2, count=1)
        if t2 == t:
            break
        t = t2.strip()
    alias = None
    if "@" in t:
        t, alias = (s.strip() for s in t.split("@", 1))
        alias = clean_value(alias) or None
    t = re.sub(r"(?i)\s+(?:and|&)$", "", clean_value(t))
    if not t or not re.search(r"[A-Za-z]{2,}", t) or len(t.split()) > 7:
        return None, alias
    return t, alias


def _split_name_and_designation(pre: str) -> Tuple[str, Optional[str]]:
    """'J Doe SIP Northtown PS' → ('J Doe', 'SIP Northtown PS')."""
    text = clean_value(pre)
    # Strip serials / honorifics before looking for a leading designation
    for _ in range(3):
        stripped = _HONORIFIC.sub("", _SERIAL_PREFIX.sub("", text, count=1), count=1).strip()
        if stripped == text:
            break
        text = stripped
    lead = _DESIGNATION.match(text)
    if lead:
        # 'SI J. Doe' / 'Inspector J. Roe' — designation before the name
        rest = text[lead.end():].strip(" ,.")
        name_part = rest.split(",", 1)[0]
        tail = rest.split(",", 1)[1].strip() if "," in rest else ""
        desig = clean_value(text[: lead.end()] + (f", {tail}" if tail else ""))
        return name_part, desig
    cut = len(text)
    for sep in (",", "("):  # a comma or parenthesis never belongs to a name
        pos = text.find(sep)
        if 0 < pos < cut:
            cut = pos
    m = _DESIGNATION.search(text)
    if m and m.start() > 0 and m.start() < cut:
        cut = m.start()
    name_part = text[:cut]
    rest = clean_value(text[cut:])
    if rest.startswith("(") and rest.endswith(")") and rest.count("(") == 1:
        rest = clean_value(rest[1:-1])
    return name_part, rest or None


def _classify_remark(remark: str) -> str:
    if _ARREST_WORDS.search(remark):
        return "arrest"
    if _ROLE_WORDS.search(remark):
        return "role"
    if _DESIGNATION.search(remark):
        return "designation"
    return "detail"


def parse_person(
    text: str,
    *,
    page: Optional[int] = None,
    serial: Optional[str] = None,
    role: Optional[str] = None,
) -> PersonEntity:
    flat = _flat(text)
    ent = PersonEntity(raw=flat[:800], source_page=page, serial_no=serial)
    if role:
        ent.role = clean_value(role) or None

    # Parenthetical remarks: arrest/custody notes and roles are pulled out;
    # designations stay inline ("Tahsildar (Executive Magistrate) of X").
    remarks: List[Tuple[str, str]] = []

    def take_remark(m: re.Match) -> str:
        kind = _classify_remark(m.group(1))
        if kind == "designation":
            return m.group(0)
        remarks.append((kind, clean_value(m.group(1))))
        return " "

    body = _PAREN.sub(take_remark, flat)
    body = re.sub(r"\s+", " ", body).strip()
    # Inline role at the end of a text-layout entry: "... R/o Lakeside - Eye witness"
    tail = re.search(r"\s[-–:]\s*([A-Za-z][A-Za-z ./&]{2,60})$", body)
    if tail and len(tail.group(1).split()) <= 6 and _ROLE_WORDS.search(tail.group(1)):
        if not ent.role:
            ent.role = clean_value(tail.group(1))
        body = body[: tail.start()].strip()

    markers: List[Tuple[int, int, str]] = []
    for kind, rx in (
        ("parent", _PARENT),
        ("caste", _CASTE),
        ("occ", _OCC),
        ("addr", _ADDR),
        ("phone", _PHONE_LABEL),
        ("aadhaar", _AADHAAR_LABEL),
    ):
        for m in rx.finditer(body):
            markers.append((m.start(), m.end(), kind))
    for m in _AGE.finditer(body):
        markers.append((m.start(), m.end(), "age"))
        if ent.age is None:
            ent.age = m.group(1)
    for m in _ID12.finditer(body):
        markers.append((m.start(), m.end(), "id_value"))
    for m in _PHONE.finditer(body):
        if not any(s <= m.start() < e for s, e, k in markers if k == "id_value"):
            markers.append((m.start(), m.end(), "phone_value"))
    markers.sort()

    first = markers[0][0] if markers else len(body)
    name_part, designation = _split_name_and_designation(body[:first])
    ent.name, ent.alias = clean_name(name_part)
    if designation:
        ent.occupation = designation

    leftovers: List[str] = []
    for idx, (start, end, kind) in enumerate(markers):
        nxt = markers[idx + 1][0] if idx + 1 < len(markers) else len(body)
        segment = body[end:nxt].strip()
        value = clean_value(segment)
        if kind == "parent":
            parent = clean_value(value.split(",")[0])
            if parent and ent.parent_or_spouse is None:
                ent.parent_or_spouse = f"{_relation(body[start:end])} {parent}"
            continue
        if kind == "caste":
            if value and ent.caste is None:
                ent.caste = clean_value(value.split(",")[0])
            continue
        if kind == "occ":
            if value:
                ent.occupation = f"{ent.occupation}; {value}" if ent.occupation else value
            continue
        if kind == "addr":
            # A stray list number after the sentence end ("Northtown. 4.") is not address
            addr = clean_value(re.sub(r"\.\s+\d{1,2}\.?\s*$", ".", segment))
            if addr and ent.address is None:
                ent.address = addr[:200]
            continue
        # Markers without a trailing free-text value: the number itself is the
        # value; any text after it up to the next marker is unlabeled detail.
        if kind == "phone":
            m = re.match(r"\+?\d[\d\s-]{6,14}\d", segment)
            if m and ent.contact is None:
                ent.contact = re.sub(r"[\s-]", "", m.group(0))
            segment = segment[m.end():] if m else segment
        elif kind == "aadhaar":
            m = _ID12.match(segment)
            if m:
                ent.identification = re.sub(r"\s", "", m.group(0))
            segment = segment[m.end():] if m else segment
        elif kind == "id_value" and ent.identification is None:
            ent.identification = re.sub(r"\s", "", body[start:end])
        elif kind == "phone_value" and ent.contact is None:
            ent.contact = re.sub(r"\D", "", body[start:end])[-10:]
        if clean_value(segment):
            leftovers.append(clean_value(segment))

    if ent.age is None:
        m = _AGE_BARE.search(body)
        if m:
            ent.age = m.group(1)

    for kind, remark in remarks:
        if kind == "arrest":
            ent.arrest_status = f"{ent.arrest_status}; {remark}" if ent.arrest_status else remark
        elif kind == "role" and not ent.role:
            ent.role = remark
        else:
            leftovers.append(remark)

    if not ent.role:
        rm = _ROLE_WORDS.search(" ".join(leftovers))
        if rm:
            ent.role = clean_value(rm.group(0))
    leftover_text = clean_value("; ".join(l for l in leftovers if clean_value(l)))
    if leftover_text and not re.fullmatch(r"[\d\s./-]+", leftover_text):
        ent.details = leftover_text[:200]
    return ent


def _mask_parens(text: str) -> str:
    return _PAREN.sub(lambda m: " " * len(m.group(0)), text)


def split_accused(text: str) -> List[Tuple[Optional[str], str]]:
    """Split an accused block into (serial, text) per person.

    Recognises A1 / A-1) / A1. markers and numbered lines. Parenthetical
    remarks are masked while splitting so '(A1&A2 were arrested)' is not
    mistaken for two new accused.
    """
    soft = soften_pdf_text(text or "")
    masked = _mask_parens(soft)
    marks = [(m.start(), m.group(1)) for m in _ACCUSED_MARK.finditer(masked)]
    if len(marks) < 1:
        marks = []
        expected = 1
        for m in _NUMBERED_LINE.finditer(masked):
            if int(m.group(1)) == expected:
                marks.append((m.start(), m.group(1)))
                expected += 1
    if not marks:
        return [(None, soft)] if clean_value(soft) else []
    chunks: List[Tuple[Optional[str], str]] = []
    if marks[0][0] > 0 and clean_value(soft[: marks[0][0]]) and re.search(r"(?i)\b(?:s/o|d/o|w/o|age)\b", soft[: marks[0][0]]):
        chunks.append((None, soft[: marks[0][0]]))
    for i, (pos, serial) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(soft)
        chunks.append((serial, soft[pos:end]))
    return chunks


def extract_accused(items: Sequence) -> List[PersonEntity]:
    """Accused records from list items (each item may hold several persons)."""
    people: List[PersonEntity] = []
    for item in items:
        chunks = split_accused(item.text)
        for serial, chunk in chunks:
            if serial is None and len(chunks) == 1:
                serial = item.serial  # text layouts: the list line's own number
            ent = parse_person(chunk, page=item.page, serial=serial)
            if not ent.name:
                continue
            people.append(ent)
    _attach_cross_remarks(people)
    return _dedupe(people)


def _attach_cross_remarks(people: List[PersonEntity]) -> None:
    """'(A1&A2 were arrested on ...)' sits after A2 but concerns A1 too."""
    by_serial = {p.serial_no: p for p in people if p.serial_no}
    for p in people:
        if not p.arrest_status:
            continue
        notes = [n.strip() for n in p.arrest_status.split(";") if n.strip()]
        keep = []
        for note in notes:
            refs = [r for r in _ACCUSED_REF.findall(note) if r in by_serial]
            if not refs or p.serial_no in refs:
                keep.append(note)
            for r in refs:
                target = by_serial[r]
                if target is not p and note not in (target.arrest_status or ""):
                    target.arrest_status = f"{target.arrest_status}; {note}" if target.arrest_status else note
        p.arrest_status = "; ".join(dict.fromkeys(keep)) or None


def extract_witnesses(items: Sequence) -> List[PersonEntity]:
    people: List[PersonEntity] = []
    for item in items:
        text = item.text
        serial = item.serial
        m = re.match(r"(?i)^\s*(?:LW|PW|CW)\s*[-–.]?\s*(\d{1,3})\b", soften_pdf_text(text))
        if m and not serial:
            serial = m.group(1)
        role = item.role
        if role is None:
            # Text layouts: role after ' : ' at the end ("... : Eyewitness")
            tail = re.search(r"\s:\s*([A-Za-z][^:]{2,80})$", _flat(text))
            if tail and _ROLE_WORDS.search(tail.group(1)):
                role = tail.group(1)
                text = _flat(text)[: tail.start()]
        ent = parse_person(text, page=item.page, serial=serial, role=role)
        if not ent.name:
            continue
        if ent.role and re.fullmatch(r"(?i)\W*do\W*", ent.role) and people and people[-1].role:
            # "-do-" (ditto) repeats the role of the previous entry
            ent.role = people[-1].role
            ent.role_note = "given as '-do-' (ditto) in the source; copied from the previous witness"
        people.append(ent)
    return _dedupe(people)


def parse_officers(value: str, page: Optional[int]) -> List[PersonEntity]:
    """IO field: one or several (numbered) officers with rank/designation."""
    soft = soften_pdf_text(value or "")
    parts = [p for p in re.split(r"(?m)(?:^|\s)(?=\(?\d{1,2}\s*[.)]\s*(?:Sri|Smt|Ms|Mr|Mrs|Dr|[A-Z]))", soft) if clean_value(p)]
    officers = []
    for part in parts or [soft]:
        ent = parse_person(part, page=page)
        if ent.name:
            officers.append(ent)
    return officers


def _dedupe(people: List[PersonEntity]) -> List[PersonEntity]:
    seen = set()
    unique: List[PersonEntity] = []
    for p in people:
        key = (compact(p.name or ""), compact(p.parent_or_spouse or ""))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


# ---------------------------------------------------------------------------
# Narrative mentions (for cross-document consistency checks)
# ---------------------------------------------------------------------------


def _trailing_name(window: str) -> Optional[str]:
    tokens = window.replace("\n", " ").split()
    picked: List[str] = []
    for tok in reversed(tokens):
        bare = tok.strip(",;:()").lower()
        if not bare:
            break
        if bare in _NAME_STOPWORDS or bare.rstrip(".") in _NAME_STOPWORDS:
            break
        serial_glued = re.match(r"^A\s*-?\d{1,2}[-.):]+([A-Za-z].*)$", tok)
        if serial_glued:  # "A1-John" — the serial starts the name
            picked.append(serial_glued.group(1))
            break
        honorific_initial = re.match(r"(?i)^(?:sri|shri|smt|mr|mrs|ms|dr)\.?([A-Z]\.?)$", tok)
        if honorific_initial:  # "Sri.A. One" — initial glued to the honorific
            picked.append(honorific_initial.group(1))
            break
        if _HONORIFIC.fullmatch(tok + " ") or re.search(r"[\d()]", tok):
            break  # an honorific or a "(2)" / "1." marker starts the name
        if re.fullmatch(r"(?i)[sdwchr]\s*/\s*[o.]\.?|age|aged|caste|occ\.?|occupation|r/o\.?", bare):
            break  # another person's attribute marker — the name starts after it
        if len(bare) > 20 or (len(bare) > 14 and bare.isalpha() and tok[0].islower()):
            break  # glued prose ("thatishisname", "Ruralhestatedthat...")
        if tok.endswith((",", ";", ":")) and picked:
            break
        if tok.endswith(".") and len(bare.rstrip(".")) > 3 and picked:
            break  # sentence end before the name
        picked.append(tok)
        if len(picked) >= 5:
            break
    picked.reverse()
    while picked and not picked[0][:1].isupper():
        picked.pop(0)
    name, _ = clean_name(" ".join(picked))
    if not name or len(name.replace(" ", "")) < 4:
        return None
    return name


def find_person_mentions(segments: Iterable[Tuple[int, str]]) -> List[PersonMention]:
    """People introduced with parentage ('Name S/o Father, age: N') in prose.

    Segments are joined first so a name and its age on the next line are read
    together; the page of each mention is kept for source attribution.
    """
    pieces: List[str] = []
    spans: List[Tuple[int, int]] = []  # (start offset, page)
    pos = 0
    for page, raw in segments:
        flat = _flat(raw)
        spans.append((pos, page))
        pieces.append(flat)
        pos += len(flat) + 1
    text = " ".join(pieces)

    def page_at(offset: int) -> Optional[int]:
        current = None
        for start, page in spans:
            if start > offset:
                break
            current = page
        return current

    mentions: List[PersonMention] = []
    for m in _PARENT.finditer(text):
        name = _trailing_name(text[max(0, m.start() - 90) : m.start()])
        if not name:
            continue
        after = text[m.end() : m.end() + 160]
        nxt_parent = _PARENT.search(after)
        if nxt_parent:
            after = after[: nxt_parent.start()]
        parent = clean_value(re.split(r"(?i)[,;]|\bage\b|\bcaste\b|\br\s*/\s*o\b|\bocc\b", after)[0])
        parent = " ".join(parent.split()[:3]) or None
        age_m = _AGE.search(after)
        mentions.append(
            PersonMention(
                name=name,
                parent=parent,
                age=age_m.group(1) if age_m else None,
                page=page_at(m.start()),
                text=text[max(0, m.start() - 60) : m.end() + 80],
            )
        )
    return mentions
