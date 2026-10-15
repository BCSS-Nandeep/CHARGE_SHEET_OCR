"""InterrogationReportAdapter: drives the IR_OCR application in this repository, unchanged.

* ``submit`` posts the original file to the host's own ``POST /api/v1/documents/upload`` route (through an in-process
  ASGI client, so the route's validation, storage, database writes and background processing are exactly what a
  browser upload runs). The caller was already authenticated by the gateway; the API-key header is passed through so
  the host attributes the upload the same way.
* ``state`` reads the persisted result through the host's own repositories, the way ``GET .../results`` does.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from .. import INTERROGATION_REPORT
from .base import (AdapterError, AdapterUnavailable, BUSY, ExtractionAdapter, NativeState, Submission,
                   corrected_fields, ocr_status)
from ..provenance import ir_provenance


class InterrogationReportAdapter(ExtractionAdapter):
    name = "ir_ocr"
    document_type = INTERROGATION_REPORT
    supported_kinds = frozenset({"pdf", "docx"})  # what the IR_OCR upload accepts; images are not accepted by it

    def __init__(self, host_app) -> None:
        self.host_app = host_app

    async def submit(self, data: bytes, filename: str, content_type: Optional[str],
                     headers: Dict[str, str]) -> Submission:
        transport = httpx.ASGITransport(app=self.host_app, client=("127.0.0.1", 0))
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://ir-host", timeout=120) as client:
                resp = await client.post(
                    "/api/v1/documents/upload",
                    files={"file": (filename, data, content_type or "application/octet-stream")},
                    headers=headers,
                )
        except httpx.HTTPError as exc:  # pragma: no cover - in-process transport
            raise AdapterUnavailable(f"IR_OCR upload failed: {type(exc).__name__}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {"detail": resp.text[:300]}
        if resp.status_code != 202:
            raise AdapterError(body.get("detail", body) if isinstance(body, dict) else body, resp.status_code)
        return Submission(native_id=body["document_id"], status=body.get("status", "queued"), raw=body)

    def state(self, native_id: str, headers: Optional[Dict[str, str]] = None) -> NativeState:
        from app import db as dbmod
        from app.repositories.document_repository import DocumentRepository
        from app.repositories.results_repository import ResultsRepository
        from app.schemas.api_responses import ResultsResponse
        from app.services.processing_service import get_progress

        with dbmod.session_scope() as db:
            repo = DocumentRepository(db)
            doc = repo.get(native_id)
            if doc is None or doc.status == "deleted":
                return NativeState(exists=False, document_status="deleted" if doc else None)
            meta = repo.to_meta(doc)
            busy = doc.status in BUSY
            results = None if busy else ResultsRepository(db).get(native_id)
            corrections = repo.get_corrections(native_id)
            status, error = doc.status, (doc.error_message or None)
            verified, verified_by, requires_review = bool(doc.verified), doc.verified_by, bool(doc.requires_review)
            pages = doc.page_count

        payload = extraction = schema = None
        if results is not None and status != "failed":
            # exactly what GET /results returns; nothing renamed, dropped or recomputed
            payload = ResultsResponse(document=meta, results=results, field_corrections=corrections).model_dump(mode="json")
            extraction, schema = results.extraction_status.value, results.schema_version
        state = NativeState(
            document_status=status, ocr_status=ocr_status(status, extraction), extraction_status=extraction,
            error=(error[:300] if error and status == "failed" else None),
            progress=get_progress(native_id) if busy else None,
            verified=verified, verified_by=verified_by, requires_review=requires_review, page_count=pages,
            payload=payload, schema_version=schema, corrections=corrected_fields(corrections),
        )
        return state

    def provenance(self) -> Dict[str, Any]:
        return ir_provenance()
