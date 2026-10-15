"""ChargeSheetAdapter: drives the unmodified CHARGE_SHEET_OCR snapshot through its own HTTP API.

The worker owns the charge sheet's OCR results, corrections and verification (its own database); this adapter only
submits the original file and reads the persisted state, so the native result is carried through exactly as the
service serialises it.
"""

from __future__ import annotations

import anyio
import httpx
from typing import Any, Dict, Optional

from .. import CHARGE_SHEET
from ..provenance import charge_sheet_provenance
from .base import (AdapterError, AdapterUnavailable, BUSY, ExtractionAdapter, NativeState, Submission,
                   corrected_fields, ocr_status)
from .worker import WorkerManager


class ChargeSheetAdapter(ExtractionAdapter):
    name = "charge_sheet_ocr"
    document_type = CHARGE_SHEET
    supported_kinds = frozenset({"pdf", "docx", "png", "jpeg", "tiff"})

    def __init__(self, manager: WorkerManager) -> None:
        self.manager = manager

    # ------------------------------------------------------------------ submit
    async def submit(self, data: bytes, filename: str, content_type: Optional[str],
                     headers: Dict[str, str]) -> Submission:
        await anyio.to_thread.run_sync(self.manager.ensure_running)
        try:
            async with httpx.AsyncClient(base_url=self.manager.url, timeout=120) as client:
                resp = await client.post(
                    "/api/v1/documents/upload",
                    files={"file": (filename, data, content_type or "application/octet-stream")},
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise AdapterUnavailable(f"Charge-sheet worker upload failed: {type(exc).__name__}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {"detail": resp.text[:300]}
        if resp.status_code != 202:
            raise AdapterError(body.get("detail", body) if isinstance(body, dict) else body, resp.status_code)
        return Submission(native_id=body["document_id"], status=body.get("status", "queued"), raw=body)

    # ------------------------------------------------------------------- probe
    async def probe(self, data: bytes, filename: str, headers: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """Ask the worker's read-only classification route (text of the first pages, OCR when there is no text layer,
        and the snapshot's own ``DocumentDetector`` verdict). None when the worker cannot answer."""
        try:
            await anyio.to_thread.run_sync(self.manager.ensure_running)
            async with httpx.AsyncClient(base_url=self.manager.url, timeout=self.manager.settings.probe_timeout) as client:
                resp = await client.post(
                    "/internal/probe",
                    files={"file": (filename, data, "application/octet-stream")},
                    data={"pages": str(self.manager.settings.max_probe_pages),
                          "ocr": "true" if self.manager.settings.classify_ocr else "false"},
                    headers=headers,
                )
            return resp.json() if resp.status_code == 200 else None
        except (AdapterError, httpx.HTTPError, ValueError):
            return None

    # ------------------------------------------------------------------- state
    def state(self, native_id: str, headers: Optional[Dict[str, str]] = None) -> NativeState:
        h = headers if headers else self.manager.internal_headers()
        try:
            with httpx.Client(base_url=self.manager.url, timeout=30) as client:
                r = client.get(f"/api/v1/documents/{native_id}", headers=h)
                if r.status_code == 404:
                    return NativeState(exists=False)
                if r.status_code != 200:
                    return NativeState(reachable=False, error=f"worker answered {r.status_code}")
                meta = r.json()["document"]
                status = meta["status"]
                payload = None
                if status in ("completed", "verified"):
                    rr = client.get(f"/api/v1/documents/{native_id}/results", headers=h)
                    if rr.status_code == 200:
                        payload = rr.json()
        except (httpx.HTTPError, ValueError, KeyError):
            return NativeState(reachable=False, error="charge-sheet worker unreachable")

        results = (payload or {}).get("results") or {}
        extraction = results.get("extraction_status")
        error = meta.get("error_message") if status == "failed" else None
        return NativeState(
            document_status=status, ocr_status=ocr_status(status, extraction), extraction_status=extraction,
            error=(error[:300] if error else None),
            progress=meta.get("progress") if status in BUSY else None,
            verified=bool(meta.get("verified")), verified_by=meta.get("verified_by"),
            requires_review=bool(meta.get("requires_review", True)), page_count=meta.get("page_count"),
            payload=payload, schema_version=results.get("schema_version") or meta.get("schema_version"),
            corrections=corrected_fields((payload or {}).get("field_corrections")),
        )

    def provenance(self) -> Dict[str, Any]:
        return charge_sheet_provenance()
