"""API request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.charge_sheet import ChargeSheetData, ReviewStatus
from app.schemas.document import DocumentMeta, DocumentStatus
from app.schemas.structured import layout_definition


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    offline_mode: bool
    database: str
    ocr_available: bool
    ocr_engine: str = "none"
    ocr_error: Optional[str] = None
    ppstructure_available: bool = False
    docling_available: bool = False
    vl_available: bool = False
    # Local vision-language model: configured, reachable, model served (read-only check)
    vlm_enabled: bool = False
    vlm_available: bool = False
    vlm_model: Optional[str] = None
    vlm_transcribe_engine: Optional[str] = None
    vlm_error: Optional[str] = None
    store_page_text: bool = False
    auth_enabled: bool = False
    auth_misconfigured: bool = False
    llm_enabled: bool
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
    document_id: Optional[str] = None


class ProcessingAcceptedResponse(BaseModel):
    document_id: str
    status: DocumentStatus
    message: str
    poll_url: str


class DocumentStatusResponse(BaseModel):
    document: DocumentMeta


class ResultsResponse(BaseModel):
    document: DocumentMeta
    results: Optional[ChargeSheetData] = None
    field_corrections: Dict[str, Any] = Field(default_factory=dict)
    # Sections / fields of results.structured (labels, order, repeated or not): the UI is built from this
    structured_layout: List[dict] = Field(default_factory=layout_definition)


class DocumentListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: List[DocumentMeta]


class FieldCorrectionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str  # PdfField.id, e.g. "p1-03"
    corrected_value: Any
    review_status: ReviewStatus = ReviewStatus.CORRECTED
    notes: Optional[str] = None


class FieldCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corrections: List[FieldCorrectionItem]


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified: bool = True
    notes: Optional[str] = None


class TemplateProfile(BaseModel):
    id: str
    fingerprint: str
    label: Optional[str] = None
    document_count: int = 0
    verified: bool = False
    created_at: datetime
    last_seen_at: Optional[datetime] = None


class TemplateListResponse(BaseModel):
    items: List[TemplateProfile]
