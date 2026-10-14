"""Structured (canonical) charge-sheet schema, version 1.0.

The uploaded document's layout is variable; this output schema is not. Every
document yields the same sections and field names. Each value carries its own
status, source page and source text, and human corrections are stored beside
(never over) the machine value.

Status values (``ValueStatus``) — the only ones ever produced:

``extracted``    value found, and found verbatim in the page text under a fitting label
``not_found``    no page states this field (never guessed)
``explicit_na``  the document itself says N/A / nil / not applicable
``illegible``    the value contains an unreadable-text marker
``ambiguous``    value present but the label near it does not fit this field
``conflicting``  different pages give different values; all candidates are kept
``unverified``   the model returned a value that could not be found in the page text

``needs_review`` is True for every status except ``extracted`` / ``explicit_na`` /
``not_found``, and for any value on a page that was read with reduced quality.
No status means "verified": only a human reviewer verifies a document.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterator, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

STRUCTURED_SCHEMA_VERSION = "1.0"
PATH_PREFIX = "s."  # correction ids for structured values: "s.case.fir_number", "s.accused.0.name"


class ValueStatus(str, Enum):
    EXTRACTED = "extracted"
    NOT_FOUND = "not_found"
    EXPLICIT_NA = "explicit_na"
    ILLEGIBLE = "illegible"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"
    UNVERIFIED = "unverified"


class DocumentStructuredStatus(str, Enum):
    EXTRACTED = "extracted"  # every page was mapped
    PARTIAL = "partial"  # some pages could not be read or mapped
    FAILED = "failed"  # no page could be mapped (e.g. model unavailable)


# ---------------------------------------------------------------- field definitions


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    kind: str = "text"  # text | id | date | year | number | section | serial
    hint: str = ""  # one line for the model
    label_re: str = ""  # a source line (or the two above it) must match this to count as correctly labelled


@dataclass(frozen=True)
class SectionDef:
    key: str
    label: str
    repeated: bool
    fields: Tuple[FieldDef, ...]
    # field keys whose (normalised) values identify a record when merging pages
    identity: Tuple[str, ...] = ()


_PERSON = (
    FieldDef("name", "Name", hint="full name only, without s/o, age or address"),
    FieldDef("parent_or_spouse", "Father / husband name", hint="the name after s/o, d/o, w/o, father or husband"),
    FieldDef("age", "Age", "number", hint="age as printed"),
    FieldDef("gender", "Gender", hint="sex/gender as printed"),
    FieldDef("address", "Address", hint="residence address as printed"),
    FieldDef("occupation", "Occupation"),
)

SECTIONS: Tuple[SectionDef, ...] = (
    SectionDef("case", "Case details", False, (
        FieldDef("district", "District", hint="district name", label_re=r"dist|zone|जिला"),
        FieldDef("police_station", "Police station", hint="police station name",
                 label_re=r"police|\bp\.?\s?s\b|thana|station|थाना"),
        FieldDef("year", "Year", "year", hint="year of the FIR / case"),
        FieldDef("fir_number", "FIR number", "id", hint="the FIR number, only if labelled FIR / First Information Report",
                 label_re=r"f\.?\s*i\.?\s*r|first\s+information"),
        FieldDef("crime_number", "Crime number", "id", hint="the crime number, only if labelled Crime No / Cr. No",
                 label_re=r"cr(?:ime)?\.?\s*(?:no|num)|crime"),
        FieldDef("fir_date", "FIR date", "date", hint="date of the FIR / registration",
                 label_re=r"date|dated|\bdt\b|registered|\bon\b"),
        FieldDef("charge_sheet_number", "Charge sheet number", "id", hint="charge sheet / final report number",
                 label_re=r"charge\s*-?\s*sheet|final\s+report|challan|\bc\.?\s*s\.?\s*(?:no|num)|\bf\.?\s*r\.?\s*(?:no|num)"),
        FieldDef("submission_date", "Charge sheet date", "date", hint="date the charge sheet / final report was submitted",
                 label_re=r"date|dated|\bdt\b|submi|forward|\bon\b"),
        FieldDef("court_name", "Court", hint="court the report is submitted to",
                 label_re=r"court|magistrate|\bj?m?fc\b|sessions|judge|न्यायालय"),
        FieldDef("jurisdiction", "Jurisdiction", hint="jurisdiction / circle / sub-division",
                 label_re=r"jurisdiction|circle|sub\s*-?\s*division|\barea\b|limits|range|sector"),
        FieldDef("final_report_type", "Final report type", hint="type of final report (charge sheet, undetected, ...)",
                 label_re=r"type|final\s*report|charge\s*-?\s*sheet|undetected|untraced|abated|false|result"),
        FieldDef("supplementary_or_original", "Original / supplementary", hint="original or supplementary",
                 label_re=r"supplementary|original|further"),
    )),
    SectionDef("complainant", "Complainant", False, _PERSON + (FieldDef("contact", "Contact", hint="phone number"),)),
    SectionDef("informant", "Informant", False, _PERSON + (FieldDef("contact", "Contact", hint="phone number"),)),
    SectionDef("acts_and_sections", "Acts and sections", True, (
        FieldDef("act", "Act", hint="the Act only, e.g. IPC, NDPS Act, BNS (no section numbers)"),
        FieldDef("section", "Section", "section", hint="section number(s) only, e.g. 307 or 8(c)"),
        FieldDef("description", "Description", hint="offence description if printed"),
    ), identity=("act", "section")),
    SectionDef("investigating_officers", "Investigating officers", True, (
        FieldDef("name", "Name"),
        FieldDef("rank", "Rank / designation", hint="rank such as SI, CI, DySP"),
    ), identity=("name",)),
    SectionDef("accused", "Accused", True, (
        FieldDef("serial_no", "Serial no.", "serial", hint="number or A1/A2 marker printed before the person"),
    ) + _PERSON + (
        FieldDef("alias", "Alias"),
        FieldDef("identification", "Identification", hint="Aadhaar / ID number if printed"),
        FieldDef("contact", "Contact", hint="phone number"),
        FieldDef("arrest_status", "Arrest status", hint="arrested / absconding / not arrested, as printed"),
        FieldDef("list_heading", "Listed under", hint="the heading of the list the person appears under, as printed"),
    ), identity=("name",)),
    SectionDef("witnesses", "Witnesses", True, (
        FieldDef("serial_no", "Serial no.", "serial", hint="number or LW/PW marker printed before the person"),
    ) + _PERSON + (
        FieldDef("contact", "Contact", hint="phone number"),
        FieldDef("role", "Role", hint="role / relation to the case as printed"),
    ), identity=("name",)),
    SectionDef("property", "Property / seizure", True, (
        FieldDef("serial_no", "Serial no.", "serial"),
        FieldDef("description", "Description", hint="item seized / property description"),
        FieldDef("quantity", "Quantity"),
        FieldDef("seizure_date", "Seizure date", "date"),
        FieldDef("seizure_place", "Seizure place"),
    ), identity=("description",)),
)

SECTION_BY_KEY: Dict[str, SectionDef] = {s.key: s for s in SECTIONS}

# Generic vocabulary that signals a section on a page (headings and labels, English with a few
# Devanagari terms). Used (a) to ask the model only about sections a page shows signs of, and
# (b) to require that a person / item sits under its own section's heading. Not specific to
# any document or template.
SECTION_SIGNALS: Dict[str, str] = {
    "case": r"district|police\s*station|\bp\.?\s?s\.?\b|\bf\.?\s?i\.?\s?r\b|crime|\bcr\.?\s*no|year|court|"
            r"charge\s*-?\s*sheet|final\s*report|jurisdiction|supplementary|original|\bdate\b|challan|"
            r"जिला|थाना",
    "complainant": r"complainant|de\s*facto|victim|aggrieved|शिकायत",
    "informant": r"informant|first\s+information",
    "acts_and_sections": r"\bacts?\b|section|\bsec\b|u/s|\bipc\b|\bbns\b|ndps|cr\.?\s*p\.?\s*c|\bbnss\b|offence|penal",
    "investigating_officers": r"investigat|\bi\.?\s?o\.?\b|inspector|\bsi\b|dysp|\bci\b|\bofficer",
    "accused": r"accused|\ba\s*-?\s*\d+\b|arrest|absconding|charge\s*-?\s*sheeted|culprit|अभियुक्त",
    "witnesses": r"witness|\bpw\b|\blw\b|\bcw\b|साक्षी",
    "property": r"propert|seiz|article|exhibit|muddemal|mudamal|recover|contraband|\bmo\b",
}

# A charge sheet is expected to give at least these (a missing one is a validation
# warning, never filled in): (path, description). "a|b" = either field.
REQUIRED = (
    ("case.police_station", "police station"),
    ("case.fir_number|case.crime_number", "FIR or crime number"),
    ("accused", "at least one accused"),
    ("acts_and_sections", "at least one act/section"),
)


def field_def(section: str, key: str) -> Optional[FieldDef]:
    sec = SECTION_BY_KEY.get(section)
    if not sec:
        return None
    return next((f for f in sec.fields if f.key == key), None)


# ---------------------------------------------------------------- data models


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    source_page: Optional[int] = None
    source_text: Optional[str] = None
    status: ValueStatus = ValueStatus.EXTRACTED
    text_engine: Optional[str] = None


class StructuredValue(BaseModel):
    """One field's value with its evidence. ``value`` is what the machine extracted;
    ``corrected_value`` is a human's replacement (kept separate, never overwriting)."""

    model_config = ConfigDict(extra="forbid")

    value: Optional[str] = None
    status: ValueStatus = ValueStatus.NOT_FOUND
    source_page: Optional[int] = None
    source_text: Optional[str] = None
    pages: List[int] = Field(default_factory=list)  # every page the value appears on
    candidates: List[Candidate] = Field(default_factory=list)  # all values seen (>1 distinct = conflict)
    text_engine: Optional[str] = None  # pdf_text | nanonets | paddleocr
    needs_review: bool = False
    note: Optional[str] = None
    corrected_value: Optional[str] = None
    review_status: str = "unreviewed"  # unreviewed | corrected | verified | rejected | needs_review
    review_notes: Optional[str] = None


class StructuredRecord(BaseModel):
    """One repeated entity (an accused, a witness, an act/section, ...)."""

    model_config = ConfigDict(extra="forbid")

    pages: List[int] = Field(default_factory=list)
    fields: Dict[str, StructuredValue] = Field(default_factory=dict)
    note: Optional[str] = None


class OtherDetail(BaseModel):
    """Information the model found that fits no schema field — kept, never dropped."""

    model_config = ConfigDict(extra="forbid")

    label: str
    value: str
    source_page: Optional[int] = None
    source_text: Optional[str] = None
    status: ValueStatus = ValueStatus.UNVERIFIED
    needs_review: bool = True


class PageStructuredInfo(BaseModel):
    """How one page was read and mapped (traceability and partial-failure reporting)."""

    model_config = ConfigDict(extra="forbid")

    page: int
    text_engine: str = "pdf_text"  # pdf_text | nanonets | paddleocr | none
    text_status: str = "ok"  # ok | partial | failed | skipped
    mapping_status: str = "ok"  # ok | partial | failed | skipped
    sections_asked: List[str] = Field(default_factory=list)  # sections this page showed signs of (the model is asked only these)
    transcribe_seconds: Optional[float] = None
    map_seconds: Optional[float] = None
    note: Optional[str] = None
    unexpected_keys: List[str] = Field(default_factory=list)


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: str = "warning"  # error | warning | info
    path: Optional[str] = None
    page: Optional[int] = None
    message: str


class StructuredValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_valid: bool = True  # False only when an *error*-severity issue exists
    issues: List[ValidationIssue] = Field(default_factory=list)


class StructuredData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = STRUCTURED_SCHEMA_VERSION
    document_status: DocumentStructuredStatus = DocumentStructuredStatus.EXTRACTED
    mapping_model: Optional[str] = None
    transcribe_engine: Optional[str] = None
    single: Dict[str, Dict[str, StructuredValue]] = Field(default_factory=dict)
    lists: Dict[str, List[StructuredRecord]] = Field(default_factory=dict)
    other_details: List[OtherDetail] = Field(default_factory=list)
    narrative_field_ids: List[str] = Field(default_factory=list)  # as-printed fields holding the running narrative
    pages: List[PageStructuredInfo] = Field(default_factory=list)
    validation: StructuredValidation = Field(default_factory=StructuredValidation)
    warnings: List[str] = Field(default_factory=list)

    # ---- addressing: "case.fir_number", "accused.0.name"

    def get_value(self, path: str) -> Optional[StructuredValue]:
        parts = path.split(".")
        try:
            if len(parts) == 2:
                return self.single[parts[0]][parts[1]]
            if len(parts) == 3:
                return self.lists[parts[0]][int(parts[1])].fields[parts[2]]
        except (KeyError, IndexError, ValueError):
            return None
        return None

    def iter_values(self) -> Iterator[Tuple[str, StructuredValue]]:
        for sec, fields in self.single.items():
            for key, v in fields.items():
                yield f"{sec}.{key}", v
        for sec, records in self.lists.items():
            for i, rec in enumerate(records):
                for key, v in rec.fields.items():
                    yield f"{sec}.{i}.{key}", v

    def counts(self) -> Dict[str, int]:
        c = {s.value: 0 for s in ValueStatus}
        review = 0
        for _, v in self.iter_values():
            c[v.status.value] += 1
            review += int(v.needs_review and not v.corrected_value)
        c["needs_review"] = review
        return c


def layout_definition() -> List[dict]:
    """Section / field definitions, so a client renders any result without hardcoded fields."""
    return [
        {
            "key": s.key,
            "label": s.label,
            "repeated": s.repeated,
            "fields": [{"key": f.key, "label": f.label, "kind": f.kind} for f in s.fields],
        }
        for s in SECTIONS
    ]


def blank_value() -> StructuredValue:
    return StructuredValue()


def blank_record(section: SectionDef) -> StructuredRecord:
    return StructuredRecord(fields={f.key: blank_value() for f in section.fields})


def blank_structured() -> StructuredData:
    data = StructuredData()
    for s in SECTIONS:
        if s.repeated:
            data.lists[s.key] = []
        else:
            data.single[s.key] = {f.key: blank_value() for f in s.fields}
    return data
