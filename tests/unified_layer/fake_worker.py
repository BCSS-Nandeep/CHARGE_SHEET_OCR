"""A stand-in for the charge-sheet worker, for tests that must not depend on the OCR stack.

It speaks the subset of the CHARGE_SHEET_OCR HTTP API the unified layer uses (upload, status, results, json, corrections,
fields, verify, reprocess, source, list, health, the results page and static files) plus ``/internal/probe``. It is a test
double only: it never extracts anything, it returns what the test configured. Tests of the *real* snapshot live in
``test_real_worker.py``.
"""

from __future__ import annotations

import socket
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


class FakeWorker:
    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.mode = "complete"            # complete | fail | hold (stay queued until release())
        self.uploads: List[Dict[str, Any]] = []
        self.requests: List[Dict[str, Any]] = []   # every request seen: method, path, key header
        self.probe_reply: Optional[Dict[str, Any]] = None
        self.probe_calls = 0
        self.app = self._build()
        self.port = 0
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    # ----------------------------------------------------------------- control
    def release(self, document_id: str, status: str = "completed") -> None:
        doc = self.docs[document_id]
        doc["status"] = status
        if status == "failed":
            doc["error_message"] = "simulated extraction failure"

    def start(self) -> str:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started and time.time() < deadline:
            time.sleep(0.02)
        assert self._server.started, "fake worker did not start"
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)

    # ------------------------------------------------------------------- app
    def _meta(self, d: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": d["id"], "original_filename": d["name"], "stored_filename": d["id"] + ".bin",
            "content_type": d["content_type"], "size_bytes": d["size"], "page_count": 2, "status": d["status"],
            "error_message": d.get("error_message"), "is_scanned": False, "template_id": None,
            "classification_confidence": 0.9, "requires_review": not d["verified"], "verified": d["verified"],
            "verified_by": d.get("verified_by"), "created_at": d["created"], "updated_at": d["created"],
            "processed_at": None, "schema_version": "2.0.0",
            "progress": {"step": "ocr", "elapsed_seconds": 1.5} if d["status"] in ("queued", "processing") else None,
        }

    def _results(self, d: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "document": self._meta(d),
            "results": {"schema_version": "2.0.0", "extraction_status": "complete", "fields": [
                {"id": "p1-01", "label": "Accused Name", "value": "RAVI KUMAR", "confidence": 0.99},
                {"id": "p1-02", "label": "Brief Facts", "value": "line one\nline two", "confidence": 0.9}]},
            "field_corrections": dict(d["corrections"]),
        }

    def _live(self, document_id: str) -> Dict[str, Any]:
        d = self.docs.get(document_id)
        if d is None or d["status"] == "deleted":
            raise HTTPException(404, "Document not found")
        return d

    def _build(self) -> FastAPI:
        app = FastAPI()
        me = self

        @app.middleware("http")
        async def record(request: Request, call_next):
            me.requests.append({"method": request.method, "path": request.url.path,
                                "key": request.headers.get("x-api-key")})
            return await call_next(request)

        @app.get("/health")
        def health():
            return {"status": "ok"}

        @app.get("/static/css/styles.css")
        def css():
            return PlainTextResponse("/* fake charge-sheet styles */", media_type="text/css")

        @app.post("/api/v1/documents/upload", status_code=202)
        async def upload(file: UploadFile = File(...)):
            data = await file.read()
            if not data:
                raise HTTPException(400, "Uploaded file is empty")
            doc_id = str(uuid.uuid4())
            d = {"id": doc_id, "name": file.filename, "content_type": file.content_type, "size": len(data),
                 "status": "queued" if me.mode == "hold" else ("failed" if me.mode == "fail" else "completed"),
                 "verified": False, "corrections": {}, "history": [], "created": _now(), "bytes": data,
                 "reprocess_count": 0}
            if me.mode == "fail":
                d["error_message"] = "simulated extraction failure"
            me.docs[doc_id] = d
            me.uploads.append({"id": doc_id, "name": file.filename, "size": len(data), "content_type": file.content_type})
            return {"document_id": doc_id, "status": d["status"], "message": "Document accepted for processing",
                    "poll_url": f"/api/v1/documents/{doc_id}"}

        @app.get("/api/v1/documents")
        def listing(limit: int = 20, offset: int = 0, status: Optional[str] = None, verified: Optional[bool] = None):
            rows = [d for d in me.docs.values() if d["status"] != "deleted"
                    and (status is None or d["status"] == status) and (verified is None or d["verified"] == verified)]
            rows.sort(key=lambda d: d["created"], reverse=True)
            return {"total": len(rows), "limit": limit, "offset": offset,
                    "items": [me._meta(d) for d in rows[offset:offset + limit]]}

        @app.get("/api/v1/documents/{document_id}")
        def get_document(document_id: str):
            return {"document": me._meta(me._live(document_id))}

        @app.get("/api/v1/documents/{document_id}/results")
        def results(document_id: str):
            d = me._live(document_id)
            if d["status"] == "failed":
                return JSONResponse({"detail": {"message": d.get("error_message"), "extraction_status": "failed"}}, 422)
            if d["status"] in ("queued", "processing"):
                return {"document": me._meta(d), "results": None, "field_corrections": {}}
            return me._results(d)

        @app.get("/api/v1/documents/{document_id}/json")
        def final_json(document_id: str):
            d = me._live(document_id)
            return {"Accused Name": d["corrections"].get("p1-01", {}).get("corrected_value", "RAVI KUMAR")}

        @app.get("/api/v1/documents/{document_id}/corrections")
        def corrections(document_id: str):
            return list(me._live(document_id)["history"])

        @app.patch("/api/v1/documents/{document_id}/fields")
        async def patch_fields(document_id: str, request: Request):
            d = me._live(document_id)
            body = await request.json()
            for c in body["corrections"]:
                d["corrections"][c["field_id"]] = {"corrected_value": c["corrected_value"], "review_status": c.get("review_status", "corrected")}
                d["history"].append({"canonical_name": c["field_id"], "corrected_value": c["corrected_value"]})
            d["verified"] = False
            return me._results(d)

        @app.post("/api/v1/documents/{document_id}/verify")
        async def verify(document_id: str, request: Request):
            d = me._live(document_id)
            body = await request.json()
            if body.get("verified", True) and not (body.get("notes") or "").strip():
                raise HTTPException(400, "Verification notes are required before marking results verified")
            d["verified"] = bool(body.get("verified", True))
            d["verified_by"] = "tester"
            d["status"] = "verified" if d["verified"] else "completed"
            d["history"].append({"canonical_name": "_verification", "corrected_value": body.get("notes")})
            return me._results(d)

        @app.post("/api/v1/documents/{document_id}/reprocess")
        def reprocess(document_id: str):
            d = me._live(document_id)
            d["reprocess_count"] += 1
            d["verified"] = False
            d["status"] = "queued" if me.mode == "hold" else "completed"
            return {"document": me._meta(d)}

        @app.delete("/api/v1/documents/{document_id}", status_code=204)
        def delete(document_id: str):
            me._live(document_id)["status"] = "deleted"
            return PlainTextResponse("", status_code=204)

        @app.get("/api/v1/documents/{document_id}/source")
        def source(document_id: str):
            d = me._live(document_id)
            from fastapi.responses import Response

            return Response(d["bytes"], media_type=d["content_type"] or "application/octet-stream")

        @app.get("/results/{document_id}", response_class=HTMLResponse)
        def page(document_id: str):
            return ('<!DOCTYPE html><html><head><link rel="stylesheet" href="/static/css/styles.css?v=1" /></head>'
                    f'<body data-page="results" data-document-id="{document_id}" data-api-key-header="X-API-Key">'
                    '<div id="results-root"></div><script src="/static/js/app.js?v=1"></script></body></html>')

        @app.get("/static/js/app.js")
        def app_js():
            return PlainTextResponse("/* fake charge-sheet app.js */", media_type="application/javascript")

        @app.post("/internal/probe")
        async def probe(file: UploadFile = File(...), pages: int = Form(2), ocr: bool = Form(True)):
            await file.read()
            me.probe_calls += 1
            if me.probe_reply is None:
                raise HTTPException(404, "no probe")
            return me.probe_reply

        return app
