"""ASGI entry of the charge-sheet worker (runs in the charge-sheet interpreter, working directory = the snapshot).

    python -m uvicorn cs_worker_entry:app --host 127.0.0.1 --port 8101      # cwd = vendor/charge_sheet_ocr

It imports the snapshot's own application object (``app.main:app``, unchanged: no file of the snapshot is edited) and
attaches ONE extra, read-only route, ``POST /internal/probe``, used by the unified layer to classify an upload:

* the text of the first pages (native text layer; PaddleOCR through the snapshot's own ``OcrService`` when a page has
  no text layer and OCR is allowed), and
* the snapshot's own ``DocumentDetector`` verdict on that text.

Nothing the probe computes is stored or fed into the extraction pipeline. This module deliberately lives outside any
package called ``app`` (that name belongs to the snapshot).
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.main import app as _charge_sheet_app
from app.config import Settings, get_settings
from app.security.auth import require_api_key
from app.security.files import validate_upload
from app.services.document_detector import DocumentDetector
from app.services.source_prep import KINDS, prepare_source

NATIVE_MIN_CHARS = 80      # a page with at least this much text is read from its text layer
MAX_TEXT_CHARS = 12000     # what is returned (classification needs the headings, not the whole document)
OCR_MAX_PAGES = 1          # CPU OCR costs about a minute per page: classify from the first textless page only
OCR_TOP_FRACTION = 0.4     # ...and only its upper part, where the form's title and first headings are
OCR_MAX_SIDE = 1400        # px of the page's longest side (the snapshot's own OCR render is ~1650 px for A4 at 200 dpi)

router = APIRouter(prefix="/internal", tags=["unified-internal"])


def _top_of_page(png: bytes) -> bytes:
    """The upper part of a rendered page (the title region) as PNG; the whole page if it cannot be cropped."""
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(png)) as im:
            out = io.BytesIO()
            im.crop((0, 0, im.width, max(1, int(im.height * OCR_TOP_FRACTION)))).save(out, "PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001
        return png


def _probe(data: bytes, kind: str, pages: int, allow_ocr: bool) -> Dict[str, Any]:
    import fitz  # PyMuPDF (a dependency of the snapshot)

    from app.services.ocr_service import OcrService
    from app.services.pdf_service import PdfService

    with tempfile.TemporaryDirectory(prefix="unified_probe_") as tmp:
        path = Path(tmp) / f"probe{KINDS[kind].ext}"
        path.write_bytes(data)
        work = prepare_source(path)  # images / DOCX -> a derived PDF beside the temp copy
        doc = fitz.open(work)
        try:
            page_count = len(doc)
            wanted = max(1, min(int(pages), page_count))
            native = [doc[i].get_text() or "" for i in range(wanted)]
        finally:
            doc.close()

        texts, source, ocr_error, timings = [], "none", None, None
        ocr = OcrService() if allow_ocr else None
        ocr_ready = bool(ocr and ocr.is_available())
        used_ocr = False
        ocr_pages = 0
        for i, text in enumerate(native):
            if len(text.strip()) >= NATIVE_MIN_CHARS:
                texts.append(text)
            elif ocr_ready and ocr_pages < OCR_MAX_PAGES:
                ocr_pages += 1
                try:
                    t0 = time.monotonic()
                    png = _top_of_page(PdfService().render_page_image(work, i + 1, max_side=OCR_MAX_SIDE))
                    t1 = time.monotonic()
                    result = ocr.recognize_png(png)
                    timings = {"render_ms": int((t1 - t0) * 1000), "ocr_ms": int((time.monotonic() - t1) * 1000)}
                    if result.error:
                        ocr_error = str(result.error)[:80]
                    texts.append(result.text)  # a property of the snapshot's OcrResult
                    used_ocr = used_ocr or bool(result.text.strip())
                except Exception as exc:  # noqa: BLE001 - classification falls back to manual selection
                    ocr_error = f"{type(exc).__name__}"
                    texts.append(text)
            else:
                texts.append(text)
        text = "\n".join(t for t in texts if t).strip()
        has_native = any(len(t.strip()) >= NATIVE_MIN_CHARS for t in native)
        if text:
            source = "native+ocr" if (used_ocr and has_native) else ("ocr" if used_ocr else "native")
        verdict = DocumentDetector().detect(text) if text else None
        return {
            "kind": kind,
            "page_count": page_count,
            "pages_examined": wanted,
            "text_source": source,
            "text_chars": len(text),
            "text": text[:MAX_TEXT_CHARS],
            "ocr_requested": bool(allow_ocr),
            "ocr_available": ocr_ready,
            "ocr_error": ocr_error,
            "ocr_timings": timings,
            "detector": None if verdict is None else {
                "document_type": verdict.document_type, "confidence": verdict.confidence,
                "section_hits": verdict.section_hits, "uncertain": verdict.uncertain, "notes": verdict.notes,
            },
        }


@router.post("/probe", dependencies=[Depends(require_api_key)])
async def probe(
    file: UploadFile = File(...),
    pages: int = Form(2),
    ocr: bool = Form(True),
    settings: Settings = Depends(get_settings),
) -> Dict[str, Any]:
    data, kind = await validate_upload(file, settings)  # same content-based file-type check as an upload
    return await run_in_threadpool(_probe, data, kind, pages, ocr)


def _exit_with_parent(pid: int) -> None:
    """A managed worker must not outlive the application that started it (a killed or crashed parent runs no shutdown
    code, and an orphan would keep the port and its old settings)."""
    import os
    import threading

    def wait() -> None:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if not handle:
                os._exit(0)  # the parent is already gone
            kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
            os._exit(0)
        while True:
            try:
                os.kill(pid, 0)
            except OSError:
                os._exit(0)
            time.sleep(2)

    threading.Thread(target=wait, name="parent-watch", daemon=True).start()


def _watch_parent_if_managed() -> None:
    import os

    pid = os.environ.get("UNIFIED_PARENT_PID", "")
    if pid.isdigit():
        _exit_with_parent(int(pid))


_charge_sheet_app.include_router(router)
_watch_parent_if_managed()
app = _charge_sheet_app
