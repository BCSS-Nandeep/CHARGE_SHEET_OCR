"""ASGI front door of the unified application.

Every request goes through here and is sent to exactly one of two apps:

* the **host** - the existing IR_OCR application object (``app.main:app``), unchanged. It still serves its own routes,
  pages and static files, and everything about interrogation reports;
* the **unified layer** (``unified.routes``) - upload, history, intakes, the combined report, person images, and the
  authenticated forwarding of charge-sheet documents to the worker.

Routing is by path, and for ``/api/v1/documents/{id}...`` / ``/results/{id}`` by the owner recorded in the registry
(ids the registry does not know belong to IR_OCR, the host - they predate the merge).

After a response is sent the gateway reacts to the host's and the worker's own answers, in background threads, never
affecting them: reprocess -> new image run + fresh combined JSON; saved corrections / verification -> fresh combined
JSON; delete -> registry tombstone. (Same idea as the IR person-image middleware, for both document types.)
"""

from __future__ import annotations

import re
import threading
from typing import Callable, List, Optional, Tuple

from .coordinator import Coordinator
from .routes import inject_assets

_ID = r"(?P<id>[0-9a-fA-F-]{36})"
_DOC = re.compile(rf"^/api/v1/documents/{_ID}(?P<rest>/.*)?$")
_RESULTS = re.compile(rf"^/results/{_ID}$")
_UNIFIED_REST = re.compile(r"^/(?:person-images(?:/.*)?|report_result)$")
_REPROCESS = re.compile(r"^/reprocess$")
_CHANGE = re.compile(r"^/(?:fields|verify)$")
_UNIFIED_PREFIXES = ("/api/v1/unified/", "/unified-static/", "/cs-static/")


def classify_request(method: str, path: str, owner_of: Callable[[str], Optional[str]],
                    charge_sheet_owner: str) -> Tuple[str, Optional[str]]:
    """('unified' | 'host' | 'host_results', document_id)"""
    if path == "/" and method == "GET":
        return "unified", None
    if path == "/api/v1/documents/upload" and method == "POST":
        return "unified", None
    if path == "/api/v1/documents" and method == "GET":
        return "unified", None
    if path.startswith(_UNIFIED_PREFIXES):
        return "unified", None
    m = _DOC.match(path)
    if m:
        document_id, rest = m.group("id"), m.group("rest") or ""
        if _UNIFIED_REST.match(rest):
            return "unified", document_id
        return ("unified" if owner_of(document_id) == charge_sheet_owner else "host"), document_id
    m = _RESULTS.match(path)
    if m and method == "GET":
        document_id = m.group("id")
        return ("unified" if owner_of(document_id) == charge_sheet_owner else "host_results"), document_id
    return "host", None


def followup(method: str, path: str) -> Tuple[Optional[str], Optional[str]]:
    """What a finished request means for the unified layer: ('reprocess'|'change'|'delete', document_id)."""
    m = _DOC.match(path)
    if not m:
        return None, None
    rest, document_id = m.group("rest") or "", m.group("id")
    if method == "POST" and _REPROCESS.match(rest):
        return "reprocess", document_id
    if method in ("PATCH", "POST") and _CHANGE.match(rest):
        return "change", document_id
    if method == "DELETE" and rest == "":
        return "delete", document_id
    return None, None


def _in_thread(fn: Callable[[], None]) -> None:
    def run() -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - never let the follow-up work surface in either application
            pass

    threading.Thread(target=run, name="unified-followup", daemon=True).start()


class Gateway:
    def __init__(self, host_app, unified_app, coordinator: Coordinator) -> None:
        self.host_app = host_app
        self.unified_app = unified_app
        self.coord = coordinator

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            return await self.unified_app(scope, receive, send)
        if scope["type"] != "http":
            return await self.host_app(scope, receive, send)

        method, path = scope.get("method", ""), scope.get("path", "")
        target, document_id = classify_request(method, path, self.coord.owner, self.coord.cs.name)
        kind, followup_id = followup(method, path)

        if target == "host_results":  # the IR results page, plus the unified banner / Person Images tab
            return await self._with_injection(scope, receive, send)
        app = self.unified_app if target == "unified" else self.host_app
        if kind is None:
            return await app(scope, receive, send)

        status = {"code": 0}

        async def watch(message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                self._after(kind, followup_id, status["code"])

        await app(scope, receive, watch)

    def _after(self, kind: str, document_id: str, code: int) -> None:
        if kind == "reprocess" and 200 <= code < 300:
            _in_thread(lambda: self.coord.on_reprocess(document_id))
        elif kind == "change" and code == 200:
            _in_thread(lambda: self.coord.on_change(document_id))
        elif kind == "delete" and code in (200, 204):
            _in_thread(lambda: self.coord.on_delete(document_id))

    async def _with_injection(self, scope, receive, send) -> None:
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
            names = {k.lower(): v for k, v in headers}
            if (start.get("status") == 200 and b"text/html" in names.get(b"content-type", b"")
                    and b"content-encoding" not in names):
                body = inject_assets(body)
                headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
                headers.append((b"content-length", str(len(body)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.host_app(scope, receive, capture)
