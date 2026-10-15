"""Runs person-image extraction for a stored document, independently of OCR.

``start(document_id)`` queues a new run (one at a time: the OpenCV detector is shared, as in the original).
It reads the SAME stored upload the OCR pipeline uses (read-only), writes only under the document's output folder,
records the outcome in the manifest, then refreshes ``report_result.json``. Whatever happens in here is contained:
an exception becomes a ``failed`` run in the manifest, and OCR results are never touched.
"""

from __future__ import annotations

import hashlib
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

from app import db as dbmod
from app.config import get_settings
from app.repositories.document_repository import DocumentRepository
from app.schemas.document import DocumentStatus
from app.security.files import resolve_under

from . import report_result, store

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="person-images")
_detect_lock = threading.Lock()  # one detection at a time (shared OpenCV detector)
_futures: List[Future] = []

_PATH_LIKE = re.compile(r"(?:[A-Za-z]:)?[\\/][^\s'\"]+")


def _scrub(message: str) -> str:
    """An error summary that is safe to show: no file system paths, bounded length."""
    return _PATH_LIKE.sub("<path>", message or "")[:200]


def start(document_id: str) -> str:
    """Queue a new extraction run for the document and return its run id. Previous runs are kept."""
    run_id = store.new_run_id()
    store.add_run(document_id, {
        "run_id": run_id, "status": store.QUEUED, "queued_at": store.utcnow(), "started_at": None,
        "finished_at": None, "error": None, "total_images_found": 0, "skipped_images": 0,
        "unreadable_images": 0, "source_format": None, "source_sha256": None, "images": [],
    }, report_result.original_name(document_id))
    report_result.refresh_quietly(document_id)
    _futures[:] = [f for f in _futures if not f.done()]
    _futures.append(_executor.submit(_work, document_id, run_id))
    return run_id


def wait_idle(timeout: float = 120.0) -> None:
    """Block until every queued run has finished (tests, shutdown)."""
    for f in list(_futures):
        f.result(timeout=timeout)


def _finish(document_id: str, run_id: str, status: str, **fields) -> None:
    store.update_run(document_id, run_id, status=status, finished_at=store.utcnow(), **fields)
    report_result.refresh_quietly(document_id)


def _work(document_id: str, run_id: str) -> None:
    try:
        store.update_run(document_id, run_id, status=store.PROCESSING, started_at=store.utcnow())
        report_result.refresh_quietly(document_id)

        with dbmod.session_scope() as db:  # read-only lookup of the stored upload
            doc = DocumentRepository(db).get(document_id)
            if doc is None or doc.status == DocumentStatus.DELETED.value:
                return _finish(document_id, run_id, store.FAILED, error="document not found")
            name, stored = doc.original_filename or "", doc.stored_filename

        suffix = Path(name).suffix.lower()
        from .extractor import SUPPORTED_EXTENSIONS

        if suffix not in SUPPORTED_EXTENSIONS:
            return _finish(document_id, run_id, store.UNSUPPORTED,
                           error=f"image extraction supports {', '.join(SUPPORTED_EXTENSIONS)} only",
                           source_format=suffix.lstrip(".") or None)
        path = resolve_under(get_settings().upload_dir, stored)
        if not path.is_file():
            return _finish(document_id, run_id, store.FAILED, error="source file is not available",
                           source_format=suffix.lstrip("."))
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()

        from . import extractor

        run = next(r for r in store.read_manifest(document_id)["runs"] if r["run_id"] == run_id)
        out = store.run_dir(document_id, run)
        base = store.image_base_name(name, out)
        with _detect_lock:
            result = extractor.save_person_images(name, data, out, base_name=base)

        images = [
            {"file": store.relative_ref(run["folder"], im["name"]), "name": im["name"], "index": i,
             "source_page": im["page"], "source_member": im["source_member"], "width": im["width"],
             "height": im["height"], "faces": im["faces"], "size_bytes": im["size"], "sha256": im["sha256"]}
            for i, im in enumerate(result["saved"], start=1)
        ]
        unreadable = result["unreadable"]
        if images:
            status = store.PARTIAL if unreadable else store.SUCCEEDED
        else:
            status = store.PARTIAL if unreadable else store.NO_IMAGES
        _finish(
            document_id, run_id, status, images=images, total_images_found=result["total"],
            skipped_images=result["total"] - len(images) - unreadable, unreadable_images=unreadable,
            source_format=suffix.lstrip("."), source_sha256=sha,
            error=(f"{unreadable} image(s) could not be read" if unreadable else None),
        )
    except Exception as exc:  # noqa: BLE001 - contained: recorded as a failed run, OCR is unaffected
        try:
            _finish(document_id, run_id, store.FAILED, error=_scrub(f"{type(exc).__name__}: {exc}"))
        except Exception:  # noqa: BLE001
            pass


def recover_interrupted() -> int:
    """Runs left queued/processing by a restart can never finish: mark them failed (nothing is removed)."""
    count = 0
    for document_id, run_id in store.interrupted_runs():
        store.update_run(document_id, run_id, status=store.FAILED, finished_at=store.utcnow(),
                         error="interrupted by a restart; run the image extraction again")
        count += 1
    return count
