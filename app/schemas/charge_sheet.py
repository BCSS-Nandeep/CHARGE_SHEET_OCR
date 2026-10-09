"""Versioned charge-sheet schema with traceable field evidence."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


SCHEMA_VERSION = "1.0.0"


class ReviewStatus(str, Enum):
    UNREVIEWED = "unreviewed"
    NEEDS_REVIEW = "needs_review"
    CORRECTED = "corrected"
    VERIFIED = "verified"
    REJECTED = "rejected"


class ValidationStatus(str, Enum):
    PENDING = "pending"
    VALID = "valid"
    INVALID = "invalid"
    MISSING = "missing"
    UNCERTAIN = "uncertain"
    CONFLICTING = "conflicting"


class SourceConflict(BaseModel):
    """Unresolved disagreement between source mentions — never auto-resolved."""

    model_config = ConfigDict(extra="forbid")

    field_name: str
    values: List[str] = Field(default_factory=list)
    source_pages: List[int] = Field(default_factory=list)
    evidence: Optional[str] = None
    resolution: str = "unresolved"


class ExtractedField(BaseModel):
    """Single extracted value with provenance and review metadata."""

    model_config = ConfigDict(extra="forbid")

    canonical_name: str
    original_label: Optional[str] = None
    original_value: Optional[str] = None
    normalized_value: Optional[Any] = None
    source_page: Optional[int] = None
    source_text: Optional[str] = None
    source_location: Optional[str] = None
    extraction_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    mapping_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    validation_status: ValidationStatus = ValidationStatus.PENDING
    review_status: ReviewStatus = ReviewStatus.UNREVIEWED
    corrected_value: Optional[Any] = None
    notes: Optional[str] = None
    alternate_values: List[str] = Field(default_factory=list)


class PersonDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[ExtractedField] = None
    alias: Optional[ExtractedField] = None
    parent_or_spouse: Optional[ExtractedField] = None
    age: Optional[ExtractedField] = None
    gender: Optional[ExtractedField] = None
    address: Optional[ExtractedField] = None
    occupation: Optional[ExtractedField] = None
    identification: Optional[ExtractedField] = None
    role: Optional[ExtractedField] = None
    additional_fields: List[ExtractedField] = Field(default_factory=list)


class AccusedPerson(PersonDetails):
    serial_no: Optional[ExtractedField] = None
    arrest_status: Optional[ExtractedField] = None
    custody_status: Optional[ExtractedField] = None
    charges: List[ExtractedField] = Field(default_factory=list)


class Witness(PersonDetails):
    serial_no: Optional[ExtractedField] = None
    witness_type: Optional[ExtractedField] = None
    statement_summary: Optional[ExtractedField] = None


class LegalSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    act: Optional[ExtractedField] = None
    section: Optional[ExtractedField] = None
    description: Optional[ExtractedField] = None


class SeizedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    serial_no: Optional[ExtractedField] = None
    description: Optional[ExtractedField] = None
    quantity: Optional[ExtractedField] = None
    seizure_date: Optional[ExtractedField] = None
    seizure_place: Optional[ExtractedField] = None
    custody_details: Optional[ExtractedField] = None


class LabResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lab_name: Optional[ExtractedField] = None
    report_number: Optional[ExtractedField] = None
    report_date: Optional[ExtractedField] = None
    findings: Optional[ExtractedField] = None
    related_property: Optional[ExtractedField] = None


class DocumentTypeInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: Optional[ExtractedField] = None
    classification_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    classification_notes: Optional[str] = None
    template_fingerprint: Optional[str] = None
    template_id: Optional[str] = None
    previously_seen: bool = False


class CaseIdentifiers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    district: Optional[ExtractedField] = None
    police_station: Optional[ExtractedField] = None
    year: Optional[ExtractedField] = None
    fir_number: Optional[ExtractedField] = None
    crime_number: Optional[ExtractedField] = None
    fir_date: Optional[ExtractedField] = None
    charge_sheet_number: Optional[ExtractedField] = None
    submission_date: Optional[ExtractedField] = None
    court_name: Optional[ExtractedField] = None
    jurisdiction: Optional[ExtractedField] = None
    final_report_type: Optional[ExtractedField] = None
    supplementary_or_original: Optional[ExtractedField] = None


class ChargeSheetData(BaseModel):
    """Canonical charge-sheet payload (schema v1)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    extracted_at: datetime = Field(default_factory=datetime.utcnow)
    document_info: DocumentTypeInfo = Field(default_factory=DocumentTypeInfo)
    case: CaseIdentifiers = Field(default_factory=CaseIdentifiers)
    acts_and_sections: List[LegalSection] = Field(default_factory=list)
    complainant: Optional[PersonDetails] = None
    informant: Optional[PersonDetails] = None
    investigating_officer: Optional[PersonDetails] = None
    accused_persons: List[AccusedPerson] = Field(default_factory=list)
    witnesses: List[Witness] = Field(default_factory=list)
    seized_property: List[SeizedItem] = Field(default_factory=list)
    lab_results: List[LabResult] = Field(default_factory=list)
    investigation_narrative: Optional[ExtractedField] = None
    brief_facts: Optional[ExtractedField] = None
    final_conclusions: Optional[ExtractedField] = None
    unmapped_fields: List[ExtractedField] = Field(default_factory=list)
    source_conflicts: List[SourceConflict] = Field(default_factory=list)
    extraction_warnings: List[str] = Field(default_factory=list)
    overall_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    requires_human_review: bool = True
    pipeline_stages: List[str] = Field(default_factory=list)

    def all_fields(self) -> List[ExtractedField]:
        """Flatten tracked ExtractedField instances for review/stats."""
        fields: List[ExtractedField] = []

        def collect(obj: Any) -> None:
            if isinstance(obj, ExtractedField):
                fields.append(obj)
            elif isinstance(obj, BaseModel):
                for v in obj.__dict__.values():
                    collect(v)
            elif isinstance(obj, list):
                for item in obj:
                    collect(item)

        collect(self)
        return fields

    def field_map(self) -> Dict[str, ExtractedField]:
        return {f.canonical_name: f for f in self.all_fields()}
