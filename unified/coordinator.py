"""The unified document lifecycle: intake -> classification -> routing -> extraction + person images -> combined result.

The coordinator is the only place that knows both services. It owns *no* OCR data: the owning service keeps results,
corrections and verification; the registry keeps ownership, the original file and the routing decision; the image
manifest keeps the images. ``snapshot`` joins those three views for one document.

Order of an upload (``ingest``):
  1. the file type is checked from the content (unsupported files are refused, nothing is stored);
  2. the original is kept once, with its SHA-256 (the person-image pipeline reads this same file);
  3. the document type is taken from the caller (manual) or classified; when it cannot be decided with confidence the
     upload waits as an *intake* (``awaiting_type``) - it is NOT sent to any extractor;
  4. the owning adapter receives the original exactly as a direct upload would; the registry records the routing;
  5. person-image extraction starts on its own thread, independent of OCR; a watcher rebuilds ``report_result.json``
     when OCR is final.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import CHARGE_SHEET, DOCUMENT_TYPES, INTERROGATION_REPORT
from .adapters.base import AdapterError, ExtractionAdapter, NativeState
from .adapters.charge_sheet import ChargeSheetAdapter
from .adapters.interrogation import InterrogationReportAdapter
from .adapters.worker import WorkerManager
from .classification import (ACCEPTED, KIND_MEDIA, Classification, UnsupportedFile, check_file, classify,
                             file_facts, service_filename)
from .config import UnifiedSettings
from .images import ImageService
from .registry import Registry, stored_name
from .report import ReportWriter, images_section, not_applicable_section
from integrations.person_images import store

LEGACY_SOURCE = "legacy"  # a document uploaded to IR_OCR before the merge: not in the registry, owned by IR_OCR


class Coordinator:
    def __init__(self, settings: UnifiedSettings, registry: Registry, host_app,
                 manager: Optional[WorkerManager] = None) -> None:
        self.settings = settings
        self.registry = registry
        self.manager = manager or WorkerManager(settings)
        self.ir = InterrogationReportAdapter(host_app)
        self.cs = ChargeSheetAdapter(self.manager)
        self.adapters: Dict[str, ExtractionAdapter] = {self.ir.name: self.ir, self.cs.name: self.cs}
        self.by_type: Dict[str, ExtractionAdapter] = {a.document_type: a for a in self.adapters.values()}
        self.report = ReportWriter(self.snapshot)
        self.images = ImageService(self._source_of, self.report.refresh_quietly, self._name_of)
        settings.originals_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def auth_headers(request_headers, host_settings) -> Dict[str, str]:
        """The caller's API key (when sent), to pass through to the owning service unchanged."""
        key = request_headers.get(host_settings.api_key_header)
        return {host_settings.api_key_header: key} if key else {}

    def _original_path(self, stored_original: str) -> Path:
        root = self.settings.originals_dir.resolve()
        candidate = (root / Path(stored_original).name).resolve()
        if candidate.parent != root:
            raise ValueError("invalid stored original")
        return candidate

    def _legacy(self, document_id: str) -> Optional[Dict[str, Any]]:
        """A registry-shaped record for an IR_OCR document that predates the registry (None if IR_OCR has none)."""
        from app import db as dbmod
        from app.repositories.document_repository import DocumentRepository

        try:
            with dbmod.session_scope() as db:
                doc = DocumentRepository(db).get(document_id)
                if doc is None or doc.status == "deleted":
                    return None
                return {
                    "document_id": document_id, "intake_id": None, "original_filename": doc.original_filename,
                    "stored_original": doc.stored_filename, "detected_kind": None, "content_type": doc.content_type,
                    "size_bytes": doc.size_bytes, "sha256": None, "document_type": INTERROGATION_REPORT,
                    "type_source": LEGACY_SOURCE, "adapter": self.ir.name, "native_id": document_id,
                    "classification": None, "uploaded_by": None, "created_at": doc.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "deleted": False,
                }
        except Exception:  # noqa: BLE001
            return None

    def document(self, document_id: str) -> Optional[Dict[str, Any]]:
        if not store.DOC_ID.match(document_id or ""):
            return None
        row = self.registry.get_document(document_id)
        if row is not None:
            return None if row["deleted"] else row
        return self._legacy(document_id)

    def owner(self, document_id: str) -> Optional[str]:
        """Adapter name owning the id (registry first; unknown ids are left to IR_OCR, the host)."""
        return self.registry.adapter_of(document_id)

    def _name_of(self, document_id: str) -> str:
        doc = self.document(document_id)
        return doc["original_filename"] if doc else f"document {document_id[:8]}"

    def _source_of(self, document_id: str) -> Optional[Tuple[str, Path]]:
        """(file name, original bytes' path) for the image pipeline."""
        doc = self.document(document_id)
        if doc is None:
            return None
        if doc["type_source"] == LEGACY_SOURCE:
            from app.config import get_settings
            from app.security.files import resolve_under

            return doc["original_filename"], resolve_under(get_settings().upload_dir, doc["stored_original"])
        kind = doc.get("detected_kind") or "pdf"
        return service_filename(doc["original_filename"], kind), self._original_path(doc["stored_original"])

    @staticmethod
    def _require_support(adapter: ExtractionAdapter, kind: str) -> None:
        if not adapter.supports(kind):
            accepted = ", ".join(sorted(k.upper() for k in adapter.supported_kinds))
            raise AdapterError(f"{adapter.name} does not accept {kind.upper()} files (it accepts {accepted}).", 415)

    # ----------------------------------------------------------------- snapshot
    def snapshot(self, document_id: str) -> Optional[Dict[str, Any]]:
        """One document across the three views (registry, owning service, image manifest). None if unknown."""
        doc = self.document(document_id)
        if doc is None:
            return None
        adapter = self.adapters[doc["adapter"]]
        state = adapter.state(doc["native_id"])
        if not state.exists and state.reachable:
            return None
        events = self.registry.events(document_id) if doc["type_source"] != LEGACY_SOURCE else []
        applies = doc["document_type"] == INTERROGATION_REPORT
        summary = store.summarize(store.read_manifest(document_id)) if applies else {"latest": None}
        return {
            "document": doc, "state": state,
            "person_images": images_section(document_id) if applies else not_applicable_section(),
            "image_latest_run": summary["latest"],
            "warnings": [{"stage": e["stage"], "message": e["message"], "at": e["ts"]} for e in events if e["level"] == "warning"],
            "errors": [{"stage": e["stage"], "message": e["message"], "at": e["ts"]} for e in events if e["level"] == "error"],
            "provenance": adapter.provenance(),
        }

    def status_view(self, document_id: str) -> Optional[Dict[str, Any]]:
        """The compact coordinator view served by ``GET /api/v1/unified/documents/{id}``."""
        snap = self.snapshot(document_id)
        if snap is None:
            return None
        doc, state = snap["document"], snap["state"]
        images = snap["person_images"]
        return {
            "document_id": document_id,
            "original_filename": doc["original_filename"],
            "document_type": doc["document_type"],
            "document_type_source": doc["type_source"],
            "adapter": doc["adapter"],
            "native_id": doc["native_id"],
            "intake_id": doc.get("intake_id"),
            "pipelines": {
                "ocr": {"status": state.ocr_status, "document_status": state.document_status,
                        "extraction_status": state.extraction_status, "progress": state.progress,
                        "error": state.error, "reachable": state.reachable, "schema_version": state.schema_version,
                        "page_count": state.page_count},
                "person_images": {"status": images["status"], "images": len(images["images"]),
                                  "error": (snap["image_latest_run"] or {}).get("error")},
            },
            "review": {"verified": state.verified, "verified_by": state.verified_by,
                       "requires_review": state.requires_review, "corrected_fields": state.corrections},
            "warnings": snap["warnings"], "errors": snap["errors"],
            "provenance": snap["provenance"],
        }

    # ------------------------------------------------------------------- intake
    def _probe_fn(self, headers: Dict[str, str]):
        async def probe(data: bytes, name: str) -> Optional[Dict[str, Any]]:
            return await self.cs.probe(data, name, headers or self.manager.internal_headers())

        return probe

    async def ingest(self, data: bytes, filename: str, content_type: Optional[str], headers: Dict[str, str],
                     user: Optional[str], requested_type: Optional[str] = None) -> Dict[str, Any]:
        """Accept one upload. Raises UnsupportedFile (400/415), AdapterError (the owning service's refusal)."""
        filename = (filename or "document").replace("\\", "/").split("/")[-1] or "document"
        kind = check_file(data, filename)
        if requested_type is not None and requested_type not in DOCUMENT_TYPES:
            raise ValueError(f"document_type must be one of: {', '.join(DOCUMENT_TYPES)}")
        if requested_type is not None:
            self._require_support(self.by_type[requested_type], kind)  # refuse before anything is stored

        if requested_type is None:
            cls = await classify(data, filename, content_type, probe=self._probe_fn(headers),
                                 min_confidence=self.settings.classify_min_confidence,
                                 margin=self.settings.classify_margin, pages=self.settings.max_probe_pages)
        else:  # the caller decided: record why, and note it when the content points elsewhere
            cls = await classify(data, filename, content_type, probe=None,
                                 min_confidence=self.settings.classify_min_confidence,
                                 margin=self.settings.classify_margin, pages=self.settings.max_probe_pages)
            auto = cls.document_type
            cls.warnings = [w for w in cls.warnings if "worker was not available" not in w]
            cls.document_type, cls.needs_manual, cls.decision = requested_type, False, "manual_selected"
            cls.reasons = [f"document type selected by the user: {requested_type}"]
            if auto not in (requested_type, "unknown"):
                cls.warnings.append(f"the content looks like a {auto.replace('_', ' ')}, but "
                                    f"{requested_type.replace('_', ' ')} was selected and used")

        stored = stored_name(filename, {"pdf": ".pdf", "docx": ".docx", "png": ".png", "jpeg": ".jpg", "tiff": ".tif"}[kind])
        self._original_path(stored).write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()

        if cls.needs_manual:
            intake_id = self.registry.create_intake(
                original_filename=filename, stored_original=stored, detected_kind=kind, content_type=content_type,
                size_bytes=len(data), sha256=sha, classification=cls.as_dict(), uploaded_by=user)
            return {"status": "awaiting_type", "document_id": None, "intake_id": intake_id,
                    "message": "The document type could not be decided with confidence; choose one to continue.",
                    "document_type": "unknown", "classification": cls.as_dict(),
                    "choose_url": f"/api/v1/unified/intakes/{intake_id}/route"}
        try:
            return await self._dispatch(data=data, filename=filename, kind=kind, content_type=content_type,
                                        stored=stored, sha=sha, cls=cls, document_type=cls.document_type,
                                        type_source="manual" if cls.decision == "manual_selected" else "auto",
                                        headers=headers, user=user, intake_id=None)
        except BaseException:
            self._original_path(stored).unlink(missing_ok=True)  # nothing was accepted: keep no copy of the file
            raise

    async def route_intake(self, intake_id: str, document_type: str, headers: Dict[str, str],
                           user: Optional[str]) -> Dict[str, Any]:
        """Send a waiting upload to the extractor the user chose."""
        if document_type not in DOCUMENT_TYPES:
            raise ValueError(f"document_type must be one of: {', '.join(DOCUMENT_TYPES)}")
        intake = self.registry.get_intake(intake_id)
        if intake is None:
            raise LookupError("intake not found")
        if not self.registry.claim_intake(intake_id):
            raise PermissionError(f"this upload is already {intake['status']}")
        try:
            data = self._original_path(intake["stored_original"]).read_bytes()
            cls = intake["classification"] or {}
            warnings = list(cls.get("warnings") or [])
            auto = cls.get("candidates", [{}])[0].get("document_type") if cls.get("candidates") else None
            if auto and cls.get("candidates", [{}])[0].get("score", 0) >= self.settings.classify_min_confidence \
                    and auto != document_type:
                warnings.append(f"the content looks like a {auto.replace('_', ' ')}, but "
                                f"{document_type.replace('_', ' ')} was selected and used")
            cls = {**cls, "document_type": document_type, "needs_manual": False, "decision": "manual_selected",
                   "warnings": warnings, "reasons": [f"document type selected by the user: {document_type}"]}
            result = await self._dispatch(
                data=data, filename=intake["original_filename"], kind=intake["detected_kind"],
                content_type=intake["content_type"], stored=intake["stored_original"], sha=intake["sha256"],
                cls=cls, document_type=document_type, type_source="manual", headers=headers, user=user,
                intake_id=intake_id)
        except BaseException:
            self.registry.update_intake(intake_id, status="awaiting_type")  # nothing was routed: allow another try
            raise
        self.registry.update_intake(intake_id, status="routed", document_id=result["document_id"])
        return result

    def discard_intake(self, intake_id: str) -> bool:
        """Remove a waiting upload and its stored original (personal data) without extracting it."""
        intake = self.registry.get_intake(intake_id)
        if intake is None or intake["status"] != "awaiting_type":
            return False
        if not self.registry.claim_intake(intake_id):
            return False
        try:
            self._original_path(intake["stored_original"]).unlink(missing_ok=True)
        finally:
            self.registry.update_intake(intake_id, status="discarded")
        return True

    async def _dispatch(self, *, data: bytes, filename: str, kind: str, content_type: Optional[str], stored: str,
                        sha: str, cls: Any, document_type: str, type_source: str, headers: Dict[str, str],
                        user: Optional[str], intake_id: Optional[str]) -> Dict[str, Any]:
        adapter = self.by_type[document_type]
        self._require_support(adapter, kind)
        cls_dict = cls.as_dict() if isinstance(cls, Classification) else cls
        submission = await adapter.submit(data, service_filename(filename, kind), KIND_MEDIA[kind], headers)
        document_id = submission.native_id
        self.registry.add_document(
            document_id=document_id, intake_id=intake_id, original_filename=filename, stored_original=stored,
            detected_kind=kind, content_type=content_type, size_bytes=len(data), sha256=sha,
            document_type=document_type, type_source=type_source, adapter=adapter.name, native_id=document_id,
            classification=cls_dict, uploaded_by=user)
        for warning in cls_dict.get("warnings") or []:
            self.registry.add_event(document_id, "warning", "classification", warning)
        self._start_followups(document_id)
        return {"status": submission.status, "document_id": document_id,
                "message": "Document accepted for processing", "poll_url": f"/api/v1/documents/{document_id}",
                "document_type": document_type, "document_type_source": type_source, "intake_id": intake_id,
                "classification": cls_dict}

    # ----------------------------------------------------- follow-up pipelines
    def _ocr_final(self, document_id: str) -> bool:
        doc = self.document(document_id)
        if doc is None:
            return True
        state = self.adapters[doc["adapter"]].state(doc["native_id"])
        return state.final if state.reachable else False

    def _start_followups(self, document_id: str) -> None:
        """Person images start now (own thread); the combined JSON is rebuilt when OCR is final. Neither waits for the
        other, and a failure of one never reaches the other."""
        if self.images_apply(document_id):
            try:
                self.images.start(document_id)
            except Exception as exc:  # noqa: BLE001
                self.registry.add_event(document_id, "error", "person_images", f"could not start: {type(exc).__name__}")
        else:
            self.report.refresh_quietly(document_id)  # first export (an image run writes it for the others)
        self.report.watch_ocr(document_id, self._ocr_final)

    def images_apply(self, document_id: str) -> bool:
        """Person-image extraction is for interrogation reports only; charge sheets do not get it."""
        doc = self.document(document_id)
        return doc is not None and doc["document_type"] == INTERROGATION_REPORT

    def on_reprocess(self, document_id: str) -> None:
        """OCR was re-run: a new image run (earlier runs are kept) and a fresh combined JSON when OCR ends."""
        if self.document(document_id) is None:
            return
        self._start_followups(document_id)

    def on_change(self, document_id: str) -> None:
        """Corrections / verification were saved: regenerate the export from the persisted results."""
        self.report.refresh_quietly(document_id)

    def on_delete(self, document_id: str) -> None:
        """The owning service deleted the document: tombstone it here and remove the copy of the original kept by this
        layer (the service removed its own copy; the person images and the combined JSON are left, as before)."""
        row = self.registry.get_document(document_id)
        if row is None:
            return
        self.registry.mark_deleted(document_id)
        try:
            self._original_path(row["stored_original"]).unlink(missing_ok=True)
        except (OSError, ValueError):
            self.registry.add_event(document_id, "warning", "delete", "the kept original could not be removed")

    # -------------------------------------------------------------------- list
    def list_documents(self, *, status: Optional[str], verified: Optional[bool], limit: int, offset: int,
                       headers: Dict[str, str]) -> Dict[str, Any]:
        """History across both services: each service's own list, merged newest first, annotated with the type."""
        import httpx

        from app import db as dbmod
        from app.repositories.document_repository import DocumentRepository
        from app.schemas.document import DocumentStatus

        want = offset + limit
        items: List[Dict[str, Any]] = []
        total = 0
        with dbmod.session_scope() as db:
            repo = DocumentRepository(db)
            t, rows = repo.list(status=DocumentStatus(status) if status else None, verified=verified, limit=want, offset=0)
            total += t
            items += [{**repo.to_meta(r).model_dump(mode="json"), "document_type": INTERROGATION_REPORT} for r in rows]
        if self.registry.list_documents(adapter=self.cs.name) or self.manager.healthy(timeout=1.0):
            try:
                params: Dict[str, Any] = {"limit": min(want, 200), "offset": 0}
                if status:
                    params["status"] = status
                if verified is not None:
                    params["verified"] = str(verified).lower()
                resp = httpx.get(f"{self.manager.url}/api/v1/documents", params=params,
                                 headers=headers or self.manager.internal_headers(), timeout=15)
                if resp.status_code == 200:
                    body = resp.json()
                    total += body.get("total", 0)
                    items += [{**m, "document_type": CHARGE_SHEET} for m in body.get("items", [])]
            except (httpx.HTTPError, ValueError):
                pass  # the worker is down: the charge-sheet part of the history is unavailable for now
        items.sort(key=lambda m: m.get("created_at") or "", reverse=True)
        return {"total": total, "limit": limit, "offset": offset, "items": items[offset:offset + limit]}

    def pending_intakes(self) -> List[Dict[str, Any]]:
        return [{"intake_id": i["intake_id"], "original_filename": i["original_filename"],
                 "detected_kind": i["detected_kind"], "created_at": i["created_at"],
                 "candidates": (i["classification"] or {}).get("candidates", []),
                 "reasons": (i["classification"] or {}).get("reasons", [])}
                for i in self.registry.list_intakes("awaiting_type")]


__all__ = ["Coordinator", "UnsupportedFile", "ACCEPTED", "NativeState", "file_facts"]
