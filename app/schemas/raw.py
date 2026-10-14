"""Raw extraction: every text line read from the PDF, page by page, exactly as read.

No labels, grouping, filtering or correction — watermarks and OCR specks are
included. Lines are listed top to bottom (left to right within a row) after the
page is straightened.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

RAW_FORMAT = "raw-lines-1.1"  # 1.1: line positions (bbox) and page size removed


class RawLine(BaseModel):
    # "ignore": raw JSON stored before positions were removed still loads (bbox dropped)
    model_config = ConfigDict(extra="ignore")

    line: int  # 1-based position on the page
    text: str
    source: str  # native | ocr | vlm (read from the page image by the local vision model)
    confidence: Optional[float] = None  # OCR confidence; None for text-layer lines


class RawPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page: int
    text_source: str  # native | ocr | both | none
    ocr_engine: Optional[str] = None
    ocr_confidence: Optional[float] = None
    lines: List[RawLine] = Field(default_factory=list)
    note: Optional[str] = None  # why a page has no lines (OCR failed / disabled)


class RawDocument(BaseModel):
    """Stored form (the API adds document_id and file_name)."""

    model_config = ConfigDict(extra="forbid")

    format: str = RAW_FORMAT
    page_count: int = 0
    pages: List[RawPage] = Field(default_factory=list)


class RawResponse(RawDocument):
    document_id: str
    file_name: str
