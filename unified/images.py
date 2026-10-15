"""Person-image pipeline for the unified application: independent of OCR, keyed by the unified document id.

This is the IR_OCR person-image integration's run logic (``integrations/person_images/service.py``) pointed at the
unified registry instead of the IR database, so it works for charge sheets and interrogation reports alike. The
detector (``integrations.person_images.extractor``) and the output/manifest store (``integrations.person_images.store``)
are reused as they are: same detection rules, same folders, same manifest, same API shapes.

Independence: a run reads the *original* upload kept by the registry (read-only), writes only under the document's
output folder, and records its outcome in the manifest. An exception becomes a ``failed`` run; OCR results are never
read or written here, and an OCR failure never removes images.

No face recognition or identity matching is done - only the existing face *detection* used to decide whether an
embedded image shows a person.
"""

from __future__ import annotations

import hashlib
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from integrations.person_images import store

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="unified-images")
_detect_lock = threading.Lock()  # one detection at a time (the OpenCV detector is shared)
_futures: List[Future] = []
_PATH_LIKE = re.compile(r"(?:[A-Za-z]:)?[\\/][^\s'\"]+")

# (original file name, path of the original bytes) for a document id, or None
SourceLookup = Callable[[str], Optional[Tuple[str, Path]]]
Refresh = Callable[[str], None]


def _scrub(message: str) -> str:
    """An error summary that is safe to show: no file system paths, bounded length."""
    return _PATH_LIKE.sub("<path>", message or "")[:200]


class ImageService:
    def __init__(self, source_of: SourceLookup, refresh: Refresh, name_of: Callable[[str], str]) -> None:
        self._source_of = source_of
        self._refresh = refresh
        self._name_of = name_of

    def start(self, document_id: str) -> str:
        """Queue a new run for the document and return its run id. Earlier runs are kept."""
        run_id = store.new_run_id()
        store.add_run(document_id, {
            "run_id": run_id, "status": store.QUEUED, "queued_at": store.utcnow(), "started_at": None,
            "finished_at": None, "error": None, "total_images_found": 0, "skipped_images": 0,
            "unreadable_images": 0, "source_format": None, "source_sha256": None, "images": [],
        }, self._name_of(document_id))
        self._refresh(document_id)
        _futures[:] = [f for f in _futures if not f.done()]
        _futures.append(_executor.submit(self._work, document_id, run_id))
        return run_id

    def _finish(self, document_id: str, run_id: str, status: str, **fields) -> None:
        store.update_run(document_id, run_id, status=status, finished_at=store.utcnow(), **fields)
        self._refresh(document_id)

    def _work(self, document_id: str, run_id: str) -> None:
        try:
            store.update_run(document_id, run_id, status=store.PROCESSING, started_at=store.utcnow())
            self._refresh(document_id)

            source = self._source_of(document_id)
            if source is None:
                return self._finish(document_id, run_id, store.FAILED, error="document not found")
            name, path = source
            suffix = Path(name).suffix.lower() or path.suffix.lower()
            from integrations.person_images.extractor import SUPPORTED_EXTENSIONS

            if suffix not in SUPPORTED_EXTENSIONS:
                return self._finish(document_id, run_id, store.UNSUPPORTED,
                                    error=f"image extraction supports {', '.join(SUPPORTED_EXTENSIONS)} only",
                                    source_format=suffix.lstrip(".") or None)
            if not path.is_file():
                return self._finish(document_id, run_id, store.FAILED, error="source file is not available",
                                    source_format=suffix.lstrip("."))
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()

            from integrations.person_images import extractor

            run = next(r for r in store.read_manifest(document_id)["runs"] if r["run_id"] == run_id)
            out = store.run_dir(document_id, run)
            base = store.image_base_name(name, out)
            with _detect_lock:
                result = extractor.save_person_images(name if Path(name).suffix else name + suffix, data, out,
                                                      base_name=base)
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
            self._finish(
                document_id, run_id, status, images=images, total_images_found=result["total"],
                skipped_images=result["total"] - len(images) - unreadable, unreadable_images=unreadable,
                source_format=suffix.lstrip("."), source_sha256=sha,
                error=(f"{unreadable} image(s) could not be read" if unreadable else None),
            )
        except Exception as exc:  # noqa: BLE001 - contained: recorded as a failed run, OCR is unaffected
            try:
                self._finish(document_id, run_id, store.FAILED, error=_scrub(f"{type(exc).__name__}: {exc}"))
            except Exception:  # noqa: BLE001
                pass


def wait_idle(timeout: float = 120.0) -> None:
    """Block until every queued run has finished (tests, shutdown)."""
    for f in list(_futures):
        f.result(timeout=timeout)


def recover_interrupted() -> int:
    """Runs left queued/processing by a restart can never finish: mark them failed (nothing is removed)."""
    count = 0
    for document_id, run_id in store.interrupted_runs():
        store.update_run(document_id, run_id, status=store.FAILED, finished_at=store.utcnow(),
                         error="interrupted by a restart; run the image extraction again")
        count += 1
    return count
