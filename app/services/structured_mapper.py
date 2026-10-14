"""Page text -> canonical structured fields (VLM mapping + deterministic grounding and merge).

Three separable stages, so everything except the model call is plain Python:

1. ``StructuredMapper.map_page``  asks the model (schema-constrained JSON, one page at a
   time) which items the page states. The model lists *values only*, as ``{"k", "v"}`` pairs
   (a list of findings, not a form to fill in: a small model asked to fill a form invents
   values for every empty slot).
2. ``observe_page``  checks every returned value against the page text. Source page and
   source text come from the matching line of the page - never from the model - and a
   value that is not on the page is kept but marked ``unverified``. Model placeholders
   ("Not Mentioned"), echoes of the instructions and records with no evidence on the page
   are not presented as values (the last are kept under *other details*).
3. ``merge_observations``  combines pages into the fixed schema. Equal values from several
   pages become one value (all pages listed). When exactly one value has evidence it is
   used and the other candidates stay listed and flagged; when several values have evidence
   the field is ``conflicting`` with every candidate kept. Nothing is normalised or guessed.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.config import Settings, get_settings
from app.logging_utils import get_logger
from app.schemas.structured import (
    SECTION_BY_KEY,
    SECTION_SIGNALS,
    SECTIONS,
    Candidate,
    DocumentStructuredStatus,
    OtherDetail,
    PageStructuredInfo,
    StructuredData,
    StructuredValue,
    ValueStatus,
    blank_record,
    blank_structured,
)
from app.services.text_normalize import compact
from app.services.vlm_client import VlmBadResponse, VlmClient, VlmError, VlmUnavailable
from app.services.vlm_text import split_for_mapping

logger = get_logger(__name__)

MAX_SOURCE_CHARS = 400
# What the document itself may say in place of a value (counts only when it is on the page).
_NA_VALUES = {"na", "nil", "notapplicable", "none"}
# How a model says "nothing here"; never a value unless the page literally says it.
_ABSENT = {"notmentioned", "notspecified", "notstated", "notprovided", "notgiven", "notfound", "notavailable",
           "unknown", "nodata", "empty", "null"}
_ILLEGIBLE = re.compile(r"(?i)illegible|unreadable|\?\?\?|" + chr(0xFFFD) + r"|\[\s*\.{2,}\s*\]")
# Value statuses that count as evidence-backed, and the ones that still need a human
_STRONG = {ValueStatus.EXTRACTED, ValueStatus.EXPLICIT_NA}
_FOUND_ON_PAGE = _STRONG | {ValueStatus.AMBIGUOUS}
_NO_REVIEW = _STRONG
# value-status priority when the same value is seen several times (best first)
_PRIORITY = [
    ValueStatus.EXTRACTED,
    ValueStatus.EXPLICIT_NA,
    ValueStatus.AMBIGUOUS,
    ValueStatus.UNVERIFIED,
    ValueStatus.ILLEGIBLE,
]
# A format example for the model (invented values, shown only for the sections being asked about)
EXAMPLE_PERSON = (
    'Format example (invented; not from this page): the line "7. Tara Bose d/o Mani Bose age 20 r/o Kolkata" gives '
    '{"f":[{"k":"serial_no","v":"7"},{"k":"name","v":"Tara Bose"},{"k":"parent_or_spouse","v":"Mani Bose"},'
    '{"k":"age","v":"20"},{"k":"address","v":"Kolkata"}]}.'
)
EXAMPLE_ACT = 'Format example: "u/s 12(b) XYZ Act" gives {"f":[{"k":"act","v":"XYZ Act"},{"k":"section","v":"12(b)"}]}.'
_EXAMPLE_VALUES = ("Tara Bose", "Mani Bose", "Kolkata", "XYZ Act", "12(b)")
_PERSON_SECTIONS = {"accused", "witnesses", "complainant", "informant", "investigating_officers"}
# Text the model may copy back from the instructions (never a real value unless the page has it)
_ECHO = {compact(x) for sec in SECTIONS for x in [sec.label] + [t for f in sec.fields for t in (f.label, f.hint, f.key)] if x}
_ECHO |= {compact(x) for x in _EXAMPLE_VALUES}


# ---------------------------------------------------------------- model request


def select_sections(page_text: str) -> List[Any]:
    """The sections this page shows signs of (heading / label vocabulary in ``SECTION_SIGNALS``).

    The model is asked only about these: a small model asked about every section invents
    values for the ones the page does not have. A page that is mostly non-Latin script is
    asked about everything (the English vocabulary cannot judge it); a page with no signs of
    any section is asked for case details only.
    """
    letters = [c for c in page_text if c.isalpha()]
    if letters and sum(1 for c in letters if ord(c) > 127) / len(letters) > 0.3:
        return list(SECTIONS)
    chosen = [s for s in SECTIONS if re.search(SECTION_SIGNALS[s.key], page_text, re.I)]
    return chosen or [SECTION_BY_KEY["case"]]


def build_json_schema(sections: Optional[List[Any]] = None) -> dict:
    """JSON Schema for one page: lists of ``{k, v}`` pairs with ``k`` restricted to the
    section's field names and ``v`` a non-empty string. Everything is optional."""
    sections = sections or list(SECTIONS)
    value = {"type": "string", "minLength": 1}

    def pair(section) -> dict:
        return {
            "type": "object",
            "properties": {"k": {"enum": [f.key for f in section.fields]}, "v": value},
            "required": ["k", "v"],
            "additionalProperties": False,
        }

    props: Dict[str, dict] = {}
    for sec in sections:
        cap = len(sec.fields) * 2
        if sec.repeated:
            props[sec.key] = {
                "type": "array",
                "maxItems": 60,
                "items": {
                    "type": "object",
                    "properties": {"f": {"type": "array", "minItems": 1, "maxItems": cap, "items": pair(sec)}},
                    "required": ["f"],
                    "additionalProperties": False,
                },
            }
        else:
            props[sec.key] = {"type": "array", "maxItems": cap, "items": pair(sec)}
    props["other"] = {
        "type": "array",
        "maxItems": 20,
        "items": {
            "type": "object",
            "properties": {"label": value, "value": value},
            "required": ["label", "value"],
            "additionalProperties": False,
        },
    }
    return {"type": "object", "properties": props, "additionalProperties": False}


def build_system_prompt(sections: Optional[List[Any]] = None) -> str:
    """Rules plus the field list for ``sections`` (all sections by default)."""
    return build_rules() + "\n" + build_fields_block(sections or list(SECTIONS))


def build_rules() -> str:
    """The constant part of the instructions (identical for every request, so a server that
    caches prompt prefixes reuses it together with the page text that follows)."""
    lines = [
        "You extract data from ONE page of an Indian police charge sheet (final report). The page text is given; "
        "table rows are shown as 'cell | cell'.",
        "Answer with JSON listing ONLY what this page actually states:",
        "- Each item is {\"k\": field name, \"v\": the value copied exactly as printed (same spelling, digits, "
        "punctuation and date format)}.",
        "- Do not list a field the page does not state. Never write 'not mentioned', 'N/A' or a guess.",
        "- accused, witnesses, investigating_officers, acts_and_sections and property are lists: one entry per "
        "person or item, holding that item's own fields. Never mix two people.",
        "- fir_number and crime_number are different: use each only when the page labels the number that way.",
        "- 'name' is only the person's name (not s/o, d/o, w/o, age or address); the name after s/o, d/o or w/o "
        "goes in parent_or_spouse.",
        "- 'act' is the Act name only; 'section' is the section number(s) only.",
        "- Anything useful that fits no field goes in 'other' as {\"label\": printed label, \"value\": value}.",
        "- A page with none of this gives {}.",
    ]
    return "\n".join(lines)


def build_fields_block(sections: List[Any]) -> str:
    """The part that changes per request: field names (and a format example) of the sections asked."""
    lines = ["Field names:"]
    for sec in sections:
        lines.append(f"{sec.key}: " + ", ".join(f.key for f in sec.fields))
    keys = {s.key for s in sections}
    if keys & _PERSON_SECTIONS:
        lines.append(EXAMPLE_PERSON)
    if "acts_and_sections" in keys:
        lines.append(EXAMPLE_ACT)
    return "\n".join(lines)


# ---------------------------------------------------------------- observations


@dataclass
class Obs:
    value: str
    status: ValueStatus
    source_text: Optional[str]
    needs_review: bool
    page: int
    text_engine: str
    note: Optional[str] = None


@dataclass
class RecordObs:
    section: str
    page: int
    fields: Dict[str, Obs] = field(default_factory=dict)


@dataclass
class PageObservations:
    page: int
    text_engine: str
    single: List[Tuple[str, str, Obs]] = field(default_factory=list)  # (section, key, obs)
    records: List[RecordObs] = field(default_factory=list)
    others: List[OtherDetail] = field(default_factory=list)
    unexpected_keys: List[str] = field(default_factory=list)
    dropped: int = 0  # placeholders / echoes ignored
    info: Optional[PageStructuredInfo] = None


def _scalar(v) -> Optional[str]:
    """A model value as a stripped string; None when it says nothing."""
    if v is None or isinstance(v, (dict, list, bool)):
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s if s and s.lower() != "null" and s not in {"-", "--"} else None


def _pairs(body: Any) -> List[Tuple[str, Any]]:
    """Key/value pairs from either answer shape: ``[{"k","v"}, ...]`` or a plain object."""
    if isinstance(body, dict):
        return list(body.items())
    out: List[Tuple[str, Any]] = []
    if isinstance(body, list):
        for p in body:
            if isinstance(p, dict) and "k" in p and "v" in p:
                out.append((str(p["k"]), p["v"]))
    return out


def _record_pairs(item: Any) -> List[Tuple[str, Any]]:
    if isinstance(item, dict) and isinstance(item.get("f"), list):
        return _pairs(item["f"])
    return _pairs(item) if isinstance(item, dict) else []


def ground_value(value: str, lines: List[str], prefer: Optional[str] = None) -> Optional[Tuple[int, str]]:
    """Find the page line(s) that contain ``value`` (spacing / punctuation ignored).

    Returns (index of the last line of the match, source text) or None. When several
    lines match, a line that also contains ``prefer`` (the record's name) wins.
    """
    cv = compact(value)
    if not cv:
        return None
    short = len(cv) <= 3
    boundary = re.compile(r"(?<![A-Za-z0-9])" + re.escape(value.strip().lower()) + r"(?![A-Za-z0-9])") if short else None

    def hit(text: str) -> bool:
        return bool(boundary.search(text.lower())) if boundary else cv in compact(text)

    matches: List[Tuple[int, str]] = []
    for i, ln in enumerate(lines):
        if hit(ln):
            matches.append((i, ln.strip()))
    if not matches:  # a value wrapped over two or three lines
        for span in (2, 3):
            for i in range(len(lines) - span + 1):
                joined = " ".join(l.strip() for l in lines[i : i + span])
                if hit(joined):
                    matches.append((i + span - 1, joined))
            if matches:
                break
    if not matches:
        return None
    if prefer:
        cp = compact(prefer)
        for m in matches:
            if cp and cp in compact(m[1]):
                return m[0], m[1][:MAX_SOURCE_CHARS]
    return matches[0][0], matches[0][1][:MAX_SOURCE_CHARS]


def _label_ok(label_re: str, lines: List[str], idx: int, above: int = 2) -> bool:
    """The value's line, or one of the ``above`` lines before it (a header row / list heading), names this field."""
    window = " ".join(lines[max(0, idx - above) : idx + 1])
    return bool(re.search(label_re, window, re.I))


HEADING_WINDOW = 12  # lines above an item in which its section's heading must appear


def classify_value(
    value: str,
    lines: List[str],
    *,
    page: int,
    text_engine: str,
    label_re: str = "",
    prefer: Optional[str] = None,
    low_quality: bool = False,
    field_words: str = "",
    heading_re: str = "",
) -> Optional[Obs]:
    """Status of one model-returned value, decided only from the page text.

    Returns None for a value that is only the model's way of saying "nothing here" ("Not
    Mentioned", an N/A the page does not contain) or an echo of the instructions.
    ``field_words`` is a regex of the field's own label words: an N/A is only the
    document's answer for *this* field when such a word is next to it.
    """
    found = ground_value(value, lines, prefer)
    src = found[1] if found else None
    cv = compact(value)
    note: Optional[str] = None
    if not found and (cv in _ABSENT or cv in _NA_VALUES or cv in _ECHO):
        return None
    if _ILLEGIBLE.search(value):
        status = ValueStatus.ILLEGIBLE
        note = "the text contains an unreadable-text marker"
    elif not found:
        status = ValueStatus.UNVERIFIED
        note = "the value returned by the model was not found in the page text"
    elif cv in _NA_VALUES or cv in _ABSENT:
        if field_words and not _label_ok(field_words, lines, found[0]):
            status = ValueStatus.AMBIGUOUS
            note = "an N/A / not-stated marker appears on the page but no label for this field was found next to it"
        else:
            status = ValueStatus.EXPLICIT_NA
    elif label_re and not _label_ok(label_re, lines, found[0]):
        status = ValueStatus.AMBIGUOUS
        note = "no label for this field was found next to the value"
    elif heading_re and not _label_ok(heading_re, lines, found[0], HEADING_WINDOW):
        status = ValueStatus.AMBIGUOUS
        note = "the value is on the page, but no heading of this section was found above it"
    else:
        status = ValueStatus.EXTRACTED
    needs = status not in _NO_REVIEW
    if low_quality and not needs:
        needs, note = True, "the page was read with reduced quality"
    return Obs(value, status, src, needs, page, text_engine, note)


def _words_re(fd) -> str:
    words = {w for w in re.findall(r"[a-z]{3,}", f"{fd.label} {fd.key.replace('_', ' ')}".lower())
             if w not in {"number", "date", "the", "and", "name"}} or {fd.key.split("_")[0]}
    return "|".join(sorted(words))


def observe_page(
    raw: dict,
    page_text: str,
    *,
    page: int,
    text_engine: str,
    low_quality: bool = False,
) -> PageObservations:
    """Ground the model's answer for one page. Unknown keys are kept as other details."""
    out = PageObservations(page=page, text_engine=text_engine)
    lines = [ln for ln in page_text.splitlines() if ln.strip()]

    def classify(text, fd, prefer=None, heading="") -> Optional[Obs]:
        obs = classify_value(text, lines, page=page, text_engine=text_engine, label_re=fd.label_re if fd else "",
                             prefer=prefer, low_quality=low_quality, field_words=_words_re(fd) if fd else "",
                             heading_re=heading)
        if obs is None:
            out.dropped += 1
        return obs

    def keep_unexpected(label: str, v) -> None:
        out.unexpected_keys.append(label)
        text = _scalar(v) if not isinstance(v, (dict, list)) else _scalar(str(v))
        if text:
            obs = classify_value(text, lines, page=page, text_engine=text_engine, low_quality=low_quality)
            if obs is None:
                out.dropped += 1
                return
            out.others.append(OtherDetail(label=f"(unmapped) {label}", value=text, source_page=page,
                                          source_text=obs.source_text, status=obs.status, needs_review=True))

    for key, body in (raw or {}).items():
        if key in ("other", "other_details"):
            for item in body if isinstance(body, list) else []:
                if not isinstance(item, dict):
                    continue
                label, text = _scalar(item.get("label")), _scalar(item.get("value"))
                if not (label and text):
                    continue
                obs = classify_value(text, lines, page=page, text_engine=text_engine, low_quality=low_quality)
                if obs is None:
                    out.dropped += 1
                    continue
                out.others.append(OtherDetail(label=label, value=text, source_page=page, source_text=obs.source_text,
                                              status=obs.status, needs_review=True))
            continue
        sec = SECTION_BY_KEY.get(key)
        if sec is None:
            keep_unexpected(key, body)
            continue
        defs = {f.key: f for f in sec.fields}
        if sec.repeated:
            for item in body if isinstance(body, list) else []:
                pairs = _record_pairs(item)
                prefer = next((_scalar(v) for k, v in pairs if k == "name"), None)
                rec = RecordObs(section=sec.key, page=page)
                extra: List[Tuple[str, Obs]] = []  # a field given twice: the first is the field, the rest are kept
                for fk, fv in pairs:
                    if fk not in defs:
                        keep_unexpected(f"{key}.{fk}", fv)
                        continue
                    text = _scalar(fv)
                    obs = classify(text, defs[fk], prefer, SECTION_SIGNALS[key]) if text else None
                    if obs is None:
                        continue
                    if fk in rec.fields:
                        extra.append((fk, obs))
                    else:
                        rec.fields[fk] = obs
                if not rec.fields:
                    continue
                if not any(o.status in _FOUND_ON_PAGE for o in rec.fields.values()):
                    # nothing in this record is on the page: kept for review, but not shown as a person / item
                    for fk, o in rec.fields.items():
                        out.others.append(OtherDetail(label=f"(not on page) {key}.{fk}", value=o.value, source_page=page,
                                                      source_text=None, status=o.status, needs_review=True))
                    continue
                out.records.append(rec)
                for fk, o in extra:  # second value for the same field of the same person: a candidate of its own
                    out.others.append(OtherDetail(label=f"(second value) {key}.{fk}", value=o.value, source_page=page,
                                                  source_text=o.source_text, status=o.status, needs_review=True))
        else:
            for fk, fv in _pairs(body):
                if fk not in defs:
                    keep_unexpected(f"{key}.{fk}", fv)
                    continue
                text = _scalar(fv)
                heading = SECTION_SIGNALS[key] if key in ("complainant", "informant") else ""
                obs = classify(text, defs[fk], None, heading) if text else None
                if obs is not None:
                    out.single.append((sec.key, fk, obs))
    return out


# ---------------------------------------------------------------- the model call


class StructuredMapper:
    def __init__(self, client: Optional[VlmClient] = None, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.client = client or VlmClient(self.settings)

    def _ask(self, page: int, text: str, sections: List[Any]) -> dict:
        """One request. The page text comes first and the varying field list last, so the shared
        prefix (rules + page text) is identical across the per-section requests of a page."""
        user = f"PAGE {page} TEXT:\n{text}\n\n--\n{build_fields_block(sections)}\nReturn the JSON for this page."
        return self.client.complete_json(build_rules(), user, build_json_schema(sections))

    def _map_chunk(self, page: int, text: str, sections: List[Any], depth: int = 0) -> dict:
        """Map ``sections`` on ``text``. A cut-off answer (the model filled many fields) is retried
        smaller: first one section per request, then the text in halves; a section that is still
        cut off raises (the caller reports that part failed, not the whole page)."""
        try:
            return self._ask(page, text, sections)
        except VlmBadResponse as exc:
            if "cut off" not in exc.message:
                raise
            if len(sections) > 1:
                out: dict = {}
                for sec in sections:
                    out = _concat(out, self._map_chunk(page, text, [sec], depth))
                return out
            lines = text.splitlines()
            if depth < 2 and len(lines) > 1:
                mid = len(lines) // 2
                a = self._map_chunk(page, "\n".join(lines[:mid]), sections, depth + 1)
                b = self._map_chunk(page, "\n".join(lines[mid:]), sections, depth + 1)
                return _concat(a, b)
            raise

    def _plan(self, text: str, sections: List[Any]) -> List[List[Any]]:
        """Section groups to ask separately. A dense page is asked one section per request: the
        model fills every field it is shown, so one big answer would hit the token limit."""
        if len(sections) > 1 and len(text) > self.settings.vlm_map_split_chars:
            return [[s] for s in sections]
        return [sections]

    def map_page(self, page: int, text: str, *, text_engine: str, low_quality: bool = False) -> PageObservations:
        """Map one page. Never raises for model problems: the outcome is in ``info``."""
        started = time.monotonic()
        info = PageStructuredInfo(page=page, text_engine=text_engine)
        if len(text.strip()) < self.settings.vlm_min_page_chars:
            info.mapping_status, info.note = "skipped", "page has too little text to map"
            return PageObservations(page=page, text_engine=text_engine, info=info)
        chunks = split_for_mapping(text, self.settings.vlm_map_chunk_chars)
        sections = select_sections(text)
        info.sections_asked = [s.key for s in sections]
        merged: dict = {}
        errors: List[str] = []
        groups = self._plan(text, sections)
        # (text piece, sections) requests. A list section (accused, witnesses, ...) on a dense page is
        # asked in small pieces: its answer grows with the number of people, and an answer that hits the
        # token limit costs minutes and is thrown away, so the pieces are sized to fit under it.
        requests: List[Tuple[str, List[Any]]] = []
        for chunk in chunks:
            for group in groups:
                if len(groups) > 1 and len(group) == 1 and group[0].repeated:
                    requests += [(piece, group) for piece in split_for_mapping(chunk, self.settings.vlm_map_list_chunk_chars)]
                else:
                    requests.append((chunk, group))
        parts = len(requests)
        stop = False
        for piece, group in requests:
            try:
                merged = _concat(merged, self._map_chunk(page, piece, group))
            except VlmError as exc:
                errors.append(f"{exc.code}: {exc.message}" + (f" [{group[0].key}]" if len(groups) > 1 else ""))
                stop = isinstance(exc, VlmUnavailable)
                if stop:
                    break
        info.map_seconds = round(time.monotonic() - started, 1)
        obs = observe_page(merged, text, page=page, text_engine=text_engine, low_quality=low_quality)
        obs.info = info
        info.unexpected_keys = obs.unexpected_keys
        if obs.dropped:
            info.note = f"{obs.dropped} placeholder / instruction-echo value(s) from the model were ignored"
        if errors and (len(errors) >= parts or stop):
            info.mapping_status, info.note = "failed", "; ".join(errors)[:300]
        elif errors:
            info.mapping_status, info.note = "partial", f"{len(errors)} of {parts} parts failed: " + "; ".join(errors)[:240]
        return obs


def _concat(a: dict, b: dict) -> dict:
    """Combine two partial answers for the same page: lists are joined, objects merged.
    Chunks cover disjoint line ranges, so a single-valued field is rarely in both; when it
    is, both are kept (lists are joined)."""
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, list):
            out[k] = list(out.get(k) or []) + v
        elif isinstance(v, dict):
            cur = dict(out.get(k) or {})
            for fk, fv in v.items():
                cur.setdefault(fk, fv)
            out[k] = cur
        else:
            out.setdefault(k, v)
    return out


# ---------------------------------------------------------------- merge


def _best(group: List[Obs]) -> Obs:
    rank = {s: i for i, s in enumerate(_PRIORITY)}
    return sorted(group, key=lambda o: (rank.get(o.status, 99), o.page))[0]


def _to_candidate(o: Obs) -> Candidate:
    return Candidate(value=o.value, source_page=o.page, source_text=o.source_text, status=o.status, text_engine=o.text_engine)


def merge_values(observations: List[Obs]) -> StructuredValue:
    """One field from every page that stated it.

    * one distinct value            -> that value, all its pages;
    * exactly one value with evidence -> that value; the others stay in ``candidates`` and the
      value is flagged for review (they lack evidence: not on the page, or under another label);
    * several values with evidence, or several without any -> ``conflicting``, ``value`` empty,
      every candidate kept.
    """
    if not observations:
        return StructuredValue()
    groups: Dict[str, List[Obs]] = {}
    for o in observations:
        groups.setdefault(compact(o.value) or o.value, []).append(o)
    candidates = [_to_candidate(o) for o in observations]
    pages = sorted({o.page for o in observations})
    strong = [g for g in groups.values() if any(o.status in _STRONG for o in g)]
    chosen: Optional[List[Obs]] = None
    if len(groups) == 1:
        chosen = observations
    elif len(strong) == 1:
        chosen = strong[0]
    if chosen is None:
        return StructuredValue(
            value=None, status=ValueStatus.CONFLICTING, pages=pages, candidates=candidates, needs_review=True,
            note=f"{len(groups)} different values were found; choose the correct one or enter it",
        )
    o = _best(chosen)
    unused = sorted({x.value for x in observations if compact(x.value) != compact(o.value)})
    note = o.note
    if unused:
        listed = ", ".join(repr(u[:40]) for u in unused[:4])
        note = (f"{note}; " if note else "") + (
            f"other value(s) seen but not used because they lack evidence under a fitting label: {listed}")
    return StructuredValue(
        value=o.value, status=o.status, source_page=o.page, source_text=o.source_text,
        pages=sorted({x.page for x in chosen}), candidates=candidates, text_engine=o.text_engine,
        needs_review=o.needs_review or bool(unused), note=note,
    )


def _identity(section_key: str, rec: RecordObs) -> Optional[str]:
    sec = SECTION_BY_KEY[section_key]
    parts = [compact(rec.fields[k].value) for k in sec.identity if k in rec.fields]
    return "|".join(parts) if parts and all(parts) and len(parts) == len(sec.identity) else None


def merge_observations(
    pages: List[PageObservations],
    *,
    page_infos: List[PageStructuredInfo],
    mapping_model: Optional[str] = None,
    transcribe_engine: Optional[str] = None,
    narrative_field_ids: Optional[List[str]] = None,
    warnings: Optional[List[str]] = None,
) -> StructuredData:
    data = blank_structured()
    data.mapping_model = mapping_model
    data.transcribe_engine = transcribe_engine
    data.narrative_field_ids = list(narrative_field_ids or [])
    data.warnings = list(warnings or [])
    data.pages = sorted(page_infos, key=lambda i: i.page)

    # single-valued sections
    by_field: Dict[Tuple[str, str], List[Obs]] = {}
    for p in sorted(pages, key=lambda x: x.page):
        for sec, key, o in p.single:
            by_field.setdefault((sec, key), []).append(o)
    for (sec, key), group in by_field.items():
        data.single[sec][key] = merge_values(group)

    # repeated sections: records with the same identity on different pages are one entity
    merged: Dict[str, List[Tuple[Optional[str], List[RecordObs]]]] = {s.key: [] for s in SECTIONS if s.repeated}
    for p in sorted(pages, key=lambda x: x.page):
        created_here: set = set()  # (section, index) of entities this page started: never merged into
        for rec in p.records:
            ident = _identity(rec.section, rec)
            slot = None
            if ident is not None:
                for i, (eid, members) in enumerate(merged[rec.section]):
                    if eid == ident and (rec.section, i) not in created_here:
                        slot = i
                        break
            if slot is None:
                merged[rec.section].append((ident, [rec]))
                created_here.add((rec.section, len(merged[rec.section]) - 1))
            else:
                merged[rec.section][slot][1].append(rec)
    for sec_key, entities in merged.items():
        sec = SECTION_BY_KEY[sec_key]
        for _, members in entities:
            record = blank_record(sec)
            record.pages = sorted({m.page for m in members})
            for f in sec.fields:
                obs = [m.fields[f.key] for m in members if f.key in m.fields]
                if obs:
                    record.fields[f.key] = merge_values(obs)
            data.lists[sec_key].append(record)

    seen = set()
    for p in sorted(pages, key=lambda x: x.page):
        for od in p.others:
            k = (compact(od.label), compact(od.value))
            if k not in seen:
                seen.add(k)
                data.other_details.append(od)

    attempted = [i for i in page_infos if i.mapping_status != "skipped"]
    if not attempted:
        data.document_status = DocumentStructuredStatus.FAILED  # nothing was mapped
    elif all(i.mapping_status == "failed" for i in attempted):
        data.document_status = DocumentStructuredStatus.FAILED
    elif any(i.mapping_status in ("failed", "partial") or i.text_status in ("failed", "partial") for i in page_infos):
        data.document_status = DocumentStructuredStatus.PARTIAL
    return data
