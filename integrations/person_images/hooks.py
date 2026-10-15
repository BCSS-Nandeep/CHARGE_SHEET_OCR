"""Reacts to the OCR application's own responses; it never changes what the OCR code does or returns.

    POST /api/v1/documents/upload            202  -> start image extraction for the new document id, watch OCR
    POST /api/v1/documents/{id}/reprocess    2xx  -> start a new image-extraction run (earlier runs are kept)
    PATCH /api/v1/documents/{id}/fields      200  -> refresh report_result.json (corrections are in the database)
    POST /api/v1/documents/{id}/verify       200  -> refresh report_result.json
    GET  /results/{id}  (HTML)               200  -> add the Person Images tab's script / stylesheet to the page

The response is passed through unchanged (except the extra <script>/<link> in the results page HTML). The actions
run after the response is sent, in background threads, and any failure in them is swallowed: the OCR pipeline and its
API can neither wait for nor be broken by the image pipeline.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import report_result, service

_ID = r"(?P<id>[0-9a-fA-F-]{36})"
_UPLOAD = re.compile(r"^/api/v1/documents/upload$")
_REPROCESS = re.compile(rf"^/api/v1/documents/{_ID}/reprocess$")
_CHANGE = re.compile(rf"^/api/v1/documents/{_ID}/(?:fields|verify)$")
_RESULTS_PAGE = re.compile(rf"^/results/{_ID}$")

STATIC_URL = "/integrations/person-images/static"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def classify(method: str, path: str) -> Tuple[Optional[str], Optional[str]]:
    if method == "POST" and _UPLOAD.match(path):
        return "upload", None
    if method == "POST" and (m := _REPROCESS.match(path)):
        return "reprocess", m.group("id")
    if method in ("PATCH", "POST") and (m := _CHANGE.match(path)):
        return "change", m.group("id")
    if method == "GET" and (m := _RESULTS_PAGE.match(path)):
        return "results_page", m.group("id")
    return None, None


def _version() -> str:
    stamps = []
    for name in ("person_images.js", "person_images.css"):
        try:
            stamps.append(str(int((STATIC_DIR / name).stat().st_mtime)))
        except OSError:
            stamps.append("0")
    return "-".join(stamps)


def inject(html: bytes) -> bytes:
    """Add the tab's stylesheet and script to the results page (idempotent)."""
    if b"person_images.js" in html:
        return html
    v = _version().encode()
    css = b'<link rel="stylesheet" href="' + STATIC_URL.encode() + b'/person_images.css?v=' + v + b'" />'
    js = b'<script src="' + STATIC_URL.encode() + b'/person_images.js?v=' + v + b'" defer></script>'
    html = re.sub(rb"</head>", css + b"</head>", html, count=1, flags=re.IGNORECASE)
    return re.sub(rb"</body>", js + b"</body>", html, count=1, flags=re.IGNORECASE)


def _in_thread(fn: Callable[[], None]) -> None:
    def run() -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - never let the image pipeline surface in the OCR application
            pass

    threading.Thread(target=run, name="person-images-hook", daemon=True).start()


def after_response(kind: str, document_id: Optional[str], status: int, body: bytes) -> None:
    """The follow-up work for one of the OCR application's responses (called after it was sent)."""
    if kind == "upload" and status == 202:
        try:
            document_id = json.loads(body).get("document_id")
        except (ValueError, AttributeError):
            return
        if not document_id:
            return
        _in_thread(lambda: (service.start(document_id), report_result.watch_ocr(document_id)))
    elif kind == "reprocess" and 200 <= status < 300 and document_id:
        _in_thread(lambda: (service.start(document_id), report_result.watch_ocr(document_id)))
    elif kind == "change" and status == 200 and document_id:
        _in_thread(lambda: report_result.refresh_quietly(document_id))


class PersonImagesMiddleware:
    """Pure ASGI middleware (no BaseHTTPMiddleware: the OCR responses are forwarded as they are)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        kind, document_id = classify(scope.get("method", ""), scope.get("path", ""))
        if kind is None:
            return await self.app(scope, receive, send)

        start: dict = {}
        chunks: List[bytes] = []

        async def capture(message) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
                return
            if message["type"] != "http.response.body":
                return await send(message)
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            headers = list(start.get("headers", []))
            out = body
            names = {k.lower(): v for k, v in headers}
            if (kind == "results_page" and start.get("status") == 200 and b"text/html" in names.get(b"content-type", b"")
                    and b"content-encoding" not in names):
                out = inject(body)
                headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
                headers.append((b"content-length", str(len(out)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": out})
            if kind != "results_page":
                try:
                    after_response(kind, document_id, start.get("status", 0), body)
                except Exception:  # noqa: BLE001
                    pass

        await self.app(scope, receive, capture)
