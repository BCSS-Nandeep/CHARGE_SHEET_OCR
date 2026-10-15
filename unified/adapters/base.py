"""The common extraction-adapter interface and the neutral state both adapters report.

An adapter *drives* an existing extraction service; it never reimplements any part of it. Whatever the service
returned is carried through untouched in ``NativeState.payload``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# native document statuses (identical in both services)
BUSY = ("uploaded", "queued", "processing")
TERMINAL = ("completed", "failed", "verified", "deleted")


class AdapterError(Exception):
    """A service refused or could not be reached. ``status_code`` / ``detail`` are what to show the caller."""

    def __init__(self, detail: Any, status_code: int = 502) -> None:
        super().__init__(str(detail))
        self.detail = detail
        self.status_code = status_code


class AdapterUnavailable(AdapterError):
    """The service cannot be reached at all (worker not running / not installed)."""

    def __init__(self, detail: Any) -> None:
        super().__init__(detail, 503)


@dataclass
class Submission:
    native_id: str
    status: str
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NativeState:
    """The persisted state of one document in its owning service, read-only."""

    exists: bool = True
    reachable: bool = True
    document_status: Optional[str] = None      # uploaded|queued|processing|completed|failed|verified|deleted
    ocr_status: str = "unknown"                # processing|succeeded|partial|failed|unknown
    extraction_status: Optional[str] = None    # complete|partial|failed (the service's own value)
    error: Optional[str] = None
    progress: Optional[Dict[str, Any]] = None
    verified: bool = False
    verified_by: Optional[str] = None
    requires_review: bool = True
    page_count: Optional[int] = None
    payload: Optional[Dict[str, Any]] = None   # exactly what GET /api/v1/documents/{id}/results returns
    schema_version: Optional[str] = None       # results.schema_version, copied
    corrections: int = 0

    @property
    def final(self) -> bool:
        return (not self.exists) or self.document_status in TERMINAL


def corrected_fields(field_corrections: Optional[Dict[str, Any]]) -> int:
    """How many fields carry a saved correction. The services also store the reviewer's verification note in the same
    map under ``_verification``; that is not a corrected field."""
    return sum(1 for key in (field_corrections or {}) if not str(key).startswith("_"))


def ocr_status(document_status: Optional[str], extraction_status: Optional[str]) -> str:
    """The OCR pipeline's own status, independent of the images (same mapping as the person-image integration)."""
    if document_status in BUSY:
        return "processing"
    if document_status == "failed":
        return "failed"
    if document_status in ("completed", "verified"):
        if extraction_status == "partial":
            return "partial"
        return "failed" if extraction_status == "failed" else "succeeded"
    return "unknown"


class ExtractionAdapter(ABC):
    name: str
    document_type: str
    supported_kinds: frozenset

    def supports(self, kind: Optional[str]) -> bool:
        return kind in self.supported_kinds

    @abstractmethod
    async def submit(self, data: bytes, filename: str, content_type: Optional[str],
                     headers: Dict[str, str]) -> Submission:
        """Hand the original upload to the service exactly as a user upload would. Raises AdapterError."""

    @abstractmethod
    def state(self, native_id: str, headers: Optional[Dict[str, str]] = None) -> NativeState:
        """The latest persisted state (and native result payload when there is one)."""

    @abstractmethod
    def provenance(self) -> Dict[str, Any]:
        """Which implementation produced the results (source branch/commit, native schema)."""
