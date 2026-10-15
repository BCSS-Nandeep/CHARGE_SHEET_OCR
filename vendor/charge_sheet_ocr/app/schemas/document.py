"""Document metadata schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    VERIFIED = "verified"
    DELETED = "deleted"


class DocumentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_filename: str
    content_type: str
    size_bytes: int


class ProcessingProgress(BaseModel):
    step: str
    elapsed_seconds: float


class DocumentMeta(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    original_filename: str
    stored_filename: str
    content_type: str
    size_bytes: int
    page_count: Optional[int] = None
    status: DocumentStatus
    error_message: Optional[str] = None
    is_scanned: Optional[bool] = None
    template_id: Optional[str] = None
    classification_confidence: Optional[float] = None
    requires_review: bool = True
    verified: bool = False
    verified_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    processed_at: Optional[datetime] = None
    schema_version: Optional[str] = None
    progress: Optional[ProcessingProgress] = None  # only while this process is working on it


class DocumentFilter(BaseModel):
    status: Optional[DocumentStatus] = None
    verified: Optional[bool] = None
    limit: int = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
