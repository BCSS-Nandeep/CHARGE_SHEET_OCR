"""Versioned extraction result: the fields of one uploaded PDF, as printed.

There is no fixed list of fields. Each result holds the label / value pairs
detected in that document, in reading order, with the label and value text
exactly as they appear on the page (only missing spaces in glued PDF exports
are restored). A field that is not in the PDF is simply not in the result.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 1.x: fixed charge-sheet schema (case / accused / witnesses / acts / narrative).
# 2.0.0: per-document field list (label + value as printed, reading order).
#        1.x payloads load with an empty field list and a "reprocess" warning.
SCHEMA_VERSION = "2.0.0"
LEGACY_RESULT_WARNING = (
    "REPROCESS_REQUIRED: this result was produced by an older version with a fixed field list; "
    "reprocess the document to see the fields detected in the PDF."
)


class ExtractionStatus(str, Enum):
    """Outcome of the extraction pipeline — separate from review."""

    COMPLETE = "complete"  # every stage ran and every page produced text
    PARTIAL = "partial"  # some pages/stages failed; results are incomplete
    FAILED = "failed"  # no usable text — nothing was extracted


class StageStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"
    SKIPPED = "skipped"


class StageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    status: StageStatus = StageStatus.OK
    detail: Optional[str] = None


class PageSummary(BaseModel):
    """How text was obtained for each page (traceability for OCR vs native text)."""

    model_config = ConfigDict(extra="forbid")

    page_number: int
    text_source: str = "native"  # native | ocr | both | none
    # native_ok | ocr_ok | both_ok | partial | failed
    page_status: str = "native_ok"
    route: str = "native"  # native | ocr | both (Gate 1 decision)
    route_reasons: List[str] = Field(default_factory=list)
    char_count: int = 0
    ocr_engine: Optional[str] = None
    ocr_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    structure_engine: Optional[str] = None
    docling_used: bool = False
    vl_used: bool = False
    note: Optional[str] = None
    # Populated only when STORE_PAGE_TEXT=true (sensitive).
    native_text: Optional[str] = None
    ocr_text: Optional[str] = None


class UnresolvedItem(BaseModel):
    """A region the pipeline could not read — never invented."""

    model_config = ConfigDict(extra="forbid")

    code: str  # page_failed | page_partial | vl_unavailable | ...
    page: Optional[int] = None
    detail: str
    severity: str = "review"  # review | critical


class UnresolvedContentReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[UnresolvedItem] = Field(default_factory=list)
    needs_review: bool = False

    @property
    def empty(self) -> bool:
        return not self.items


class ReviewStatus(str, Enum):
    UNREVIEWED = "unreviewed"
    NEEDS_REVIEW = "needs_review"
    CORRECTED = "corrected"
    VERIFIED = "verified"
    REJECTED = "rejected"


class PdfField(BaseModel):
    """One label and its value, as printed in the uploaded PDF."""

    model_config = ConfigDict(extra="forbid")

    id: str  # "p1-03": page of the label + position on that page
    page: int  # page where the field starts
    pages: List[int] = Field(default_factory=list)  # every page its value runs over
    serial: Optional[str] = None  # form row number printed beside the label ("02")
    label: Optional[str] = None  # None: text that comes before any label
    value: str = ""  # lines kept; "" when the label has no value
    # header_table | form_row | label_colon | heading | text
    source: str = "text"
    # True for running text (the narrative): its lines were joined into paragraphs
    text_block: bool = False
    # Mean OCR confidence of the lines used; None for text from the PDF text layer
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    corrected_value: Optional[str] = None
    review_status: ReviewStatus = ReviewStatus.UNREVIEWED
    notes: Optional[str] = None


class DocumentTypeInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: Optional[str] = None
    classification_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    classification_notes: Optional[str] = None
    template_fingerprint: Optional[str] = None
    template_id: Optional[str] = None
    previously_seen: bool = False


class ChargeSheetData(BaseModel):
    """Extraction result for one uploaded PDF (schema v2)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    extracted_at: datetime = Field(default_factory=datetime.utcnow)
    document_info: DocumentTypeInfo = Field(default_factory=DocumentTypeInfo)
    fields: List[PdfField] = Field(default_factory=list)
    extraction_warnings: List[str] = Field(default_factory=list)
    # Mean OCR confidence over scanned pages (not accuracy); None for text-layer PDFs
    overall_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    requires_human_review: bool = True
    extraction_status: ExtractionStatus = ExtractionStatus.COMPLETE
    stage_results: List[StageResult] = Field(default_factory=list)
    page_summaries: List[PageSummary] = Field(default_factory=list)
    pipeline_stages: List[str] = Field(default_factory=list)
    unresolved_content: UnresolvedContentReport = Field(default_factory=UnresolvedContentReport)

    @model_validator(mode="before")
    @classmethod
    def _load_legacy(cls, values: Any) -> Any:
        """1.x payloads have no PDF labels to recover: keep status / pages, drop the fixed fields."""
        if not isinstance(values, dict) or str(values.get("schema_version", SCHEMA_VERSION)).startswith("2."):
            return values
        kept = {k: values[k] for k in ("extracted_at", "extraction_status", "stage_results", "pipeline_stages",
                                         "page_summaries", "unresolved_content") if k in values}
        info = values.get("document_info") or {}
        doc_type = info.get("document_type")
        kept["document_info"] = {
            "document_type": doc_type.get("original_value") if isinstance(doc_type, dict) else doc_type,
            "classification_confidence": info.get("classification_confidence"),
            "template_fingerprint": info.get("template_fingerprint"),
            "template_id": info.get("template_id"),
        }
        kept["schema_version"] = str(values.get("schema_version", "1"))
        kept["extraction_warnings"] = [LEGACY_RESULT_WARNING] + list(values.get("extraction_warnings") or [])
        return kept

    @property
    def is_legacy(self) -> bool:
        return not self.schema_version.startswith("2.")

    def field_map(self) -> Dict[str, PdfField]:
        return {f.id: f for f in self.fields}
