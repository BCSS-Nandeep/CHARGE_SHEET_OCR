"""API request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.charge_sheet import ChargeSheetData, ReviewStatus
from app.schemas.document import DocumentMeta, DocumentStatus


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    offline_mode: bool
    database: str
    ocr_available: bool
    ocr_engine: str = "none"
    ocr_error: Optional[str] = None
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


class DocumentListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: List[DocumentMeta]


class FieldCorrectionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str
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
