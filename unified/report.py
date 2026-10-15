"""The combined ``report_result.json`` (envelope version 2.0.0, see ``docs/UNIFIED_ARCHITECTURE.md``).

One file per document: ``<output>/<uploaded file name>_<ext>/report_result.json`` (the same folder as the person images).
It is derived, never a source of truth: the owning OCR service keeps the OCR results, corrections and verification, the
person-image manifest keeps the images. Rebuilding it is always safe and is done after OCR completes, after an image run
finishes, and after saved corrections / verification / reprocessing.

The OCR payload is embedded exactly as the owning service returned it (``GET /api/v1/documents/{id}/results``) with its
own ``schema_version`` - nothing renamed, dropped, recomputed or re-versioned. The envelope version is separate.

Each pipeline's status stands alone: a failed pipeline is reported as failed and the other one's data is kept.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

from integrations.person_images import store

from . import ENVELOPE_VERSION
from .provenance import unified_provenance


def images_section(document_id: str) -> Dict[str, Any]:
    """Same shape as the IR person-image integration's ``person_images`` block."""
    summary = store.summarize(store.read_manifest(document_id))
    latest, current = summary["latest"], summary["current"]
    if latest is None:
        return {"status": "not_started", "run_id": None, "total_images_found": 0, "skipped_images": 0,
                "unreadable_images": 0, "started_at": None, "finished_at": None, "images": []}
    shown = current or latest
    images = [
        {k: im.get(k) for k in ("file", "name", "source_page", "source_member", "width", "height", "faces",
                                "size_bytes", "sha256")}
        for im in (shown.get("images") or [])
    ]
    return {
        "status": latest.get("status"),  # the LATEST run's outcome (a failed re-run is not hidden)
        "run_id": shown.get("run_id"),   # whose images are listed (the newest usable run)
        "latest_run_id": latest.get("run_id"),
        "total_images_found": shown.get("total_images_found", 0),
        "skipped_images": shown.get("skipped_images", 0),
        "unreadable_images": shown.get("unreadable_images", 0),
        "started_at": shown.get("started_at"),
        "finished_at": shown.get("finished_at"),
        "images": images,
    }


def not_applicable_section() -> Dict[str, Any]:
    """Charge sheets are not put through person-image extraction."""
    return {"status": "not_applicable", "run_id": None, "total_images_found": 0, "skipped_images": 0,
            "unreadable_images": 0, "started_at": None, "finished_at": None, "images": []}


def review_section(state) -> Dict[str, Any]:
    if state is None or not state.exists:
        status = "not_available"
    elif state.verified:
        status = "verified"
    elif state.document_status in ("completed",):
        status = "needs_review" if state.requires_review else "reviewed"
    else:
        status = "not_ready"
    return {"status": status, "verified": bool(state and state.verified),
            "verified_by": state.verified_by if state else None,
            "requires_review": bool(state.requires_review) if state else None,
            "corrected_fields": state.corrections if state else 0}


def build_envelope(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """The combined document from a coordinator snapshot (see ``Coordinator.snapshot``)."""
    doc, state = snapshot["document"], snapshot["state"]
    images = snapshot["person_images"]
    latest = snapshot["image_latest_run"] or {}
    warnings = list(snapshot["warnings"])
    errors = list(snapshot["errors"])
    if state is not None and state.exists and not state.reachable:
        errors.append({"stage": "ocr", "message": state.error or "the extraction service could not be reached"})
    ocr_failed = state is not None and state.ocr_status == "failed"
    return {
        "schema_version": ENVELOPE_VERSION,
        "document_id": doc["document_id"],
        "generated_at": store.utcnow(),
        "document": {
            "original_filename": doc["original_filename"],
            "document_type": doc["document_type"],
            "document_type_source": doc["type_source"],
            "content_type": doc.get("content_type"),
            "detected_kind": doc.get("detected_kind"),
            "size_bytes": doc.get("size_bytes"),
            "sha256": doc.get("sha256"),
            "intake_id": doc.get("intake_id"),
            "registered_at": doc.get("created_at"),
        },
        "classification": doc.get("classification"),
        "processing": {
            "ocr_status": state.ocr_status if state else "unknown",
            "ocr_document_status": state.document_status if state else None,
            "ocr_extraction_status": state.extraction_status if state else None,
            "ocr_error_summary": (state.error if state and ocr_failed else None),
            "ocr_progress": state.progress if state else None,
            "image_extraction_status": images["status"],
            "image_extraction_error_summary": latest.get("error"),
            "warnings": warnings,
            "errors": errors,
        },
        "ocr": {
            "pipeline": doc["adapter"],
            "schema_version": state.schema_version if state else None,
            "payload": state.payload if state else None,
        },
        "person_images": images,
        "review": review_section(state),
        "provenance": {
            **unified_provenance(),
            "extractor": snapshot["provenance"],
            "native_ocr_schema_version": state.schema_version if state else None,
        },
    }


class ReportWriter:
    """Builds and persists the combined JSON; also watches a document's OCR until it is final."""

    def __init__(self, snapshot_fn: Callable[[str], Optional[Dict[str, Any]]]) -> None:
        self._snapshot = snapshot_fn
        self._watching: set = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list = []

    def shutdown(self, timeout: float = 10.0) -> None:
        """Stop every watcher (application shutdown, tests). Documents keep their last exported file."""
        self._stop.set()
        for thread in list(self._threads):
            thread.join(timeout=timeout)
        self._threads.clear()
        self._stop.clear()

    def build(self, document_id: str) -> Optional[Dict[str, Any]]:
        snap = self._snapshot(document_id)
        return None if snap is None else build_envelope(snap)

    def refresh(self, document_id: str) -> Optional[Dict[str, Any]]:
        """Rebuild from the latest persisted state and write ``report_result.json`` atomically."""
        snap = self._snapshot(document_id)
        if snap is None:
            return None
        data = build_envelope(snap)
        folder = store.ensure_dir(document_id, snap["document"]["original_filename"])
        store.write_json_atomic(folder / "report_result.json", data)
        return data

    def refresh_quietly(self, document_id: str) -> None:
        try:
            self.refresh(document_id)
        except Exception:  # noqa: BLE001 - background use: never affects either pipeline
            pass

    def watch_ocr(self, document_id: str, final: Callable[[str], bool], poll_seconds: float = 3.0,
                  max_seconds: float = 3 * 3600) -> threading.Thread:
        """Refresh the combined file once OCR reaches a final state (polls fast for 2 minutes, then every few s)."""
        with self._lock:
            if document_id in self._watching:
                return threading.current_thread()
            self._watching.add(document_id)

        def loop() -> None:
            try:
                started = time.monotonic()
                while time.monotonic() - started < max_seconds and not self._stop.is_set():
                    try:
                        done = final(document_id)
                    except Exception:  # noqa: BLE001
                        done = False
                    if done:
                        self.refresh_quietly(document_id)
                        return
                    self._stop.wait(1.0 if time.monotonic() - started < 120 else poll_seconds)
            finally:
                with self._lock:
                    self._watching.discard(document_id)

        thread = threading.Thread(target=loop, name=f"unified-watch-{document_id[:8]}", daemon=True)
        self._threads = [t for t in self._threads if t.is_alive()] + [thread]
        thread.start()
        return thread
