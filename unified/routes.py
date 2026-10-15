"""HTTP surface of the unified layer (a FastAPI app the gateway routes to; IR_OCR's own routes are not touched).

Authentication and roles are the host's own (``app.security.auth``): the same API keys, the same loopback-only rule when
authentication is off. Calls for charge-sheet documents are authenticated here first and then forwarded to the worker.

    GET   /                                              upload page (all file types, auto-detect or manual type)
    POST  /api/v1/documents/upload                       unified upload (same response shape as before + document_type)
    GET   /api/v1/documents                              history across both services
    POST  /api/v1/unified/classify                       classify only (nothing is stored or extracted)
    GET   /api/v1/unified/intakes                        uploads waiting for a manual type choice
    POST  /api/v1/unified/intakes/{id}/route             choose the type for a waiting upload
    DELETE /api/v1/unified/intakes/{id}                  discard a waiting upload
    GET   /api/v1/unified/documents/{id}                 coordinator view (both pipelines, review, warnings)
    GET   /api/v1/unified/health                         layer + worker status
    GET   /api/v1/documents/{id}/report_result           the combined JSON (?download=true)
    GET   /api/v1/documents/{id}/person-images[...]      person images (same API as the IR integration)
    *     /api/v1/documents/{id}[/...]                   charge-sheet documents: authenticated, then forwarded
    GET   /results/{id}                                  charge-sheet results page (IR pages are the host's)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

import anyio
import httpx
from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.logging_utils import audit
from app.security.auth import Principal, get_principal, require_api_key, require_role
from integrations.person_images import store
from integrations.person_images.routes import _MEDIA, public_view

from . import DOCUMENT_TYPES, ENVELOPE_VERSION, UNIFIED_VERSION
from .adapters.base import AdapterError
from .classification import ACCEPTED, UnsupportedFile, check_file, classify
from .coordinator import Coordinator

HERE = Path(__file__).resolve().parent
ASSET_URL = "/unified-static"
CS_STATIC_URL = "/cs-static"
_SAFE_REST = re.compile(r"^[A-Za-z0-9_\-./]*$")
_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")


class RouteRequest(BaseModel):
    document_type: str


def required_role(method: str, rest: str) -> str:
    """The role the owning service requires for the forwarded call (same rules as its own routes)."""
    if method in ("GET", "HEAD", "OPTIONS"):
        return "viewer"
    if method == "DELETE":
        return "admin"
    if method == "POST" and rest.strip("/") == "verify":
        return "reviewer"
    return "operator"


def asset_version() -> str:
    stamps = []
    for name in ("unified.js", "unified.css", "unified_images.js"):
        try:
            stamps.append(str(int((HERE / "static" / name).stat().st_mtime)))
        except OSError:
            stamps.append("0")
    return "-".join(stamps)


def inject_assets(html: bytes) -> bytes:
    """Add the unified banner / Person Images tab script and stylesheet to a results page (idempotent)."""
    if b"unified_images.js" in html:
        return html
    v = asset_version().encode()
    css = b'<link rel="stylesheet" href="' + ASSET_URL.encode() + b'/unified.css?v=' + v + b'" />'
    js = b'<script src="' + ASSET_URL.encode() + b'/unified_images.js?v=' + v + b'" defer></script>'
    html = re.sub(rb"</head>", css + b"</head>", html, count=1, flags=re.IGNORECASE)
    return re.sub(rb"</body>", js + b"</body>", html, count=1, flags=re.IGNORECASE)


def build_unified_app(coord: Coordinator) -> FastAPI:
    from app.main import _CSP_APP, _SECURITY_HEADERS  # the host's own header policy

    host_settings = get_settings()
    templates = Jinja2Templates(directory=[str(HERE / "templates"), str(HERE.parent / "app" / "templates")])
    router = APIRouter()

    def live(document_id: str) -> Dict[str, Any]:
        doc = coord.document(document_id) if store.DOC_ID.match(document_id or "") else None
        if doc is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return doc

    # ------------------------------------------------------------------ pages
    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request):
        return templates.TemplateResponse("unified_index.html", {
            "request": request, "api_key_header": host_settings.api_key_header, "app_name": "Police Document OCR",
            "docs_enabled": host_settings.api_docs_enabled,
            "asset_version": _host_asset_version(), "unified_asset_version": asset_version(),
        })

    def _host_asset_version() -> str:
        base = HERE.parent / "app" / "static"
        stamps = []
        for rel in ("js/app.js", "css/styles.css"):
            try:
                stamps.append(str(int((base / rel).stat().st_mtime)))
            except OSError:
                stamps.append("0")
        return "-".join(stamps)

    @router.get("/results/{document_id}", response_class=HTMLResponse, include_in_schema=False)
    async def charge_sheet_results_page(document_id: str):
        """The charge-sheet service's own results page (its UI is document-specific), served through this app."""
        live(document_id)
        try:
            await anyio.to_thread.run_sync(coord.manager.ensure_running)
            async with httpx.AsyncClient(base_url=coord.manager.url, timeout=30) as client:
                resp = await client.get(f"/results/{document_id}")
        except (AdapterError, httpx.HTTPError) as exc:
            return HTMLResponse(f"<h1>Charge-sheet worker unavailable</h1><p>{type(exc).__name__}</p>", status_code=503)
        if resp.status_code != 200:
            return HTMLResponse("Not found", status_code=resp.status_code)
        html = resp.content.replace(b'"/static/', f'"{CS_STATIC_URL}/'.encode())
        return HTMLResponse(inject_assets(html))

    @router.get(CS_STATIC_URL + "/{path:path}", include_in_schema=False)
    async def charge_sheet_static(path: str):
        if ".." in path.split("/") or not _SAFE_REST.match(path):
            raise HTTPException(status_code=404)
        try:
            async with httpx.AsyncClient(base_url=coord.manager.url, timeout=30) as client:
                resp = await client.get(f"/static/{path}")
        except httpx.HTTPError:
            raise HTTPException(status_code=503, detail="Charge-sheet worker unavailable")
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code)
        return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/octet-stream"))

    # ----------------------------------------------------------------- upload
    async def _read(file: UploadFile) -> bytes:
        if not file.filename:
            raise HTTPException(status_code=400, detail="Filename is required")
        chunks, total = [], 0
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > host_settings.max_upload_bytes:
                raise HTTPException(status_code=413,
                                    detail=f"File exceeds maximum size of {host_settings.max_upload_size_mb} MB")
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")
        return data

    def _requested_type(value: Optional[str]) -> Optional[str]:
        value = (value or "").strip().lower()
        if value in ("", "auto"):
            return None
        if value not in DOCUMENT_TYPES:
            raise HTTPException(status_code=400, detail=f"document_type must be 'auto' or one of: {', '.join(DOCUMENT_TYPES)}")
        return value

    async def _upload(request: Request, file: UploadFile, document_type: Optional[str], principal: Principal):
        requested = _requested_type(document_type)
        data = await _read(file)
        headers = coord.auth_headers(request.headers, host_settings)
        try:
            result = await coord.ingest(data, file.filename or "document", file.content_type, headers,
                                        principal.name, requested)
        except UnsupportedFile as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except AdapterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)
        audit("unified_upload", document_id=result.get("document_id"), intake_id=result.get("intake_id"),
              document_type=result.get("document_type"), status=result.get("status"), user=principal.name)
        return JSONResponse(result, status_code=202)

    @router.post("/api/v1/documents/upload", status_code=202)
    async def upload(request: Request, file: UploadFile = File(...), document_type: Optional[str] = Form(None),
                     principal: Principal = Depends(require_role("operator"))):
        return await _upload(request, file, document_type, principal)

    @router.post("/api/v1/unified/upload", status_code=202)
    async def upload_alias(request: Request, file: UploadFile = File(...), document_type: Optional[str] = Form(None),
                           principal: Principal = Depends(require_role("operator"))):
        return await _upload(request, file, document_type, principal)

    @router.post("/api/v1/unified/classify")
    async def classify_only(request: Request, file: UploadFile = File(...),
                            principal: Principal = Depends(require_role("operator"))):
        """Dry run: what would the upload be routed as? Nothing is stored, nothing is extracted."""
        data = await _read(file)
        try:
            check_file(data, file.filename or "")
            headers = coord.auth_headers(request.headers, host_settings)
            cls = await classify(data, file.filename or "document", file.content_type,
                                 probe=coord._probe_fn(headers), min_confidence=coord.settings.classify_min_confidence,
                                 margin=coord.settings.classify_margin, pages=coord.settings.max_probe_pages)
        except UnsupportedFile as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return cls.as_dict()

    # ---------------------------------------------------------------- intakes
    @router.get("/api/v1/unified/intakes", dependencies=[Depends(require_api_key)])
    def intakes():
        return {"items": coord.pending_intakes(), "accepted_types": ACCEPTED}

    @router.post("/api/v1/unified/intakes/{intake_id}/route", status_code=202)
    async def route_intake(intake_id: str, body: RouteRequest, request: Request,
                           principal: Principal = Depends(require_role("operator"))):
        if not _UUID.match(intake_id):
            raise HTTPException(status_code=404, detail="Upload not found")
        headers = coord.auth_headers(request.headers, host_settings)
        try:
            result = await coord.route_intake(intake_id, body.document_type.strip().lower(), headers, principal.name)
        except LookupError:
            raise HTTPException(status_code=404, detail="Upload not found")
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except AdapterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)
        audit("unified_intake_routed", document_id=result["document_id"], intake_id=intake_id,
              document_type=result["document_type"], user=principal.name)
        return JSONResponse(result, status_code=202)

    @router.delete("/api/v1/unified/intakes/{intake_id}", status_code=204, response_class=Response)
    def discard_intake(intake_id: str, principal: Principal = Depends(require_role("operator"))):
        if not _UUID.match(intake_id) or not coord.discard_intake(intake_id):
            raise HTTPException(status_code=404, detail="Upload not found or already routed")
        audit("unified_intake_discarded", intake_id=intake_id, user=principal.name)
        return Response(status_code=204)

    # --------------------------------------------------- coordinator + export
    @router.get("/api/v1/unified/documents/{document_id}", dependencies=[Depends(require_api_key)])
    def unified_status(document_id: str):
        live(document_id)
        view = coord.status_view(document_id)
        if view is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return view

    @router.get("/api/v1/unified/health", dependencies=[Depends(require_api_key)])
    def unified_health():
        return {"unified_version": UNIFIED_VERSION, "envelope_version": ENVELOPE_VERSION, "accepted_types": ACCEPTED,
                "adapters": {a.name: {"document_type": a.document_type, "kinds": sorted(a.supported_kinds)}
                             for a in coord.adapters.values()},
                "charge_sheet_worker": {"url": coord.manager.url, "managed": coord.settings.worker_manage,
                                        "healthy": coord.manager.healthy(timeout=1.5)}}

    @router.get("/api/v1/documents/{document_id}/report_result", dependencies=[Depends(require_api_key)])
    def report_result(document_id: str, download: bool = False):
        """The combined JSON, rebuilt from the latest persisted state (and saved to the document's output folder)."""
        live(document_id)
        data = coord.report.refresh(document_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Document not found")
        if not download:
            return data
        return Response(content=json.dumps(data, ensure_ascii=False, indent=2), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="report_result.json"'})

    # ----------------------------------------------------------- person images
    @router.get("/api/v1/documents/{document_id}/person-images", dependencies=[Depends(require_api_key)])
    def person_images(document_id: str):
        live(document_id)
        if not coord.images_apply(document_id):
            return {"document_id": document_id, "status": "not_applicable", "run_id": None, "latest_run_id": None,
                    "message": "Person-image extraction is not used for charge sheets.", "images": [], "runs": []}
        return public_view(document_id)

    @router.get("/api/v1/documents/{document_id}/person-images/files/{run_id}/{filename}",
                dependencies=[Depends(require_api_key)])
    def person_image_file(document_id: str, run_id: str, filename: str):
        live(document_id)
        path = store.resolve_image(document_id, run_id, filename) if coord.images_apply(document_id) else None
        if path is None:
            raise HTTPException(status_code=404, detail="Image not available")
        return FileResponse(path, media_type=_MEDIA.get(path.suffix.lower(), "application/octet-stream"),
                            filename=path.name, content_disposition_type="inline",
                            headers={"Cache-Control": "no-store"})

    @router.post("/api/v1/documents/{document_id}/person-images/run", status_code=202)
    def run_person_images(document_id: str, principal: Principal = Depends(require_role("operator"))):
        """Run the image extraction again. OCR is not touched; earlier runs and their images are kept."""
        live(document_id)
        if not coord.images_apply(document_id):
            raise HTTPException(status_code=409, detail="Person-image extraction is not used for charge sheets")
        latest = store.summarize(store.read_manifest(document_id))["latest"]
        if latest and latest.get("status") in store.ACTIVE:
            raise HTTPException(status_code=409, detail="Image extraction is already running for this document")
        run_id = coord.images.start(document_id)
        audit("person_images_run_requested", document_id=document_id, user=principal.name)
        return {"document_id": document_id, "run_id": run_id, "status": store.QUEUED}

    # ------------------------------------------------------------------ history
    @router.get("/api/v1/documents", dependencies=[Depends(require_api_key)])
    def history(request: Request, status: Optional[str] = None, verified: Optional[bool] = None,
                limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0)):
        if status is not None and status not in {"uploaded", "queued", "processing", "completed", "failed",
                                                 "verified", "deleted"}:
            raise HTTPException(status_code=422, detail="invalid status")
        headers = coord.auth_headers(request.headers, host_settings)
        return coord.list_documents(status=status, verified=verified, limit=limit, offset=offset, headers=headers)

    # --------------------------------------------- charge-sheet document proxy
    @router.api_route("/api/v1/documents/{document_id}", methods=["GET", "PATCH", "POST", "DELETE"],
                      include_in_schema=False)
    @router.api_route("/api/v1/documents/{document_id}/{rest:path}", methods=["GET", "PATCH", "POST", "DELETE"],
                      include_in_schema=False)
    async def charge_sheet_proxy(document_id: str, request: Request, rest: str = "",
                                 settings: Settings = Depends(get_settings)):
        """Calls for a charge-sheet document: authenticated against the host's keys/roles, then forwarded unchanged."""
        if not _UUID.match(document_id) or ".." in rest.split("/") or not _SAFE_REST.match(rest):
            raise HTTPException(status_code=404, detail="Not found")
        principal = get_principal(request, settings)
        require_role(required_role(request.method, rest))(principal)
        live(document_id)
        try:
            await anyio.to_thread.run_sync(coord.manager.ensure_running)
        except AdapterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)
        url = f"/api/v1/documents/{document_id}" + (f"/{rest}" if rest else "")
        headers = coord.auth_headers(request.headers, settings)
        if request.headers.get("content-type"):
            headers["content-type"] = request.headers["content-type"]
        body = await request.body()
        try:
            async with httpx.AsyncClient(base_url=coord.manager.url, timeout=120) as client:
                resp = await client.request(request.method, url, params=list(request.query_params.multi_items()),
                                            content=body, headers=headers)
        except httpx.HTTPError:
            raise HTTPException(status_code=503, detail="Charge-sheet worker unavailable")
        keep = {k: v for k, v in resp.headers.items()
                if k.lower() in ("content-type", "content-disposition", "cache-control")}
        return Response(content=resp.content, status_code=resp.status_code, headers=keep)

    # ------------------------------------------------------------------- app
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        from .images import recover_interrupted

        try:
            recover_interrupted()
        except Exception:  # noqa: BLE001
            pass
        if coord.settings.worker_manage and coord.settings.worker_autostart:
            import threading

            threading.Thread(target=_warm_worker, args=(coord,), name="cs-worker-warmup", daemon=True).start()
        yield
        coord.report.shutdown()
        coord.manager.stop()

    app = FastAPI(title="Police Document OCR - unified layer", version=UNIFIED_VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    app.mount(ASSET_URL, StaticFiles(directory=str(HERE / "static")), name="unified_static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        response.headers.setdefault("Content-Security-Policy", _CSP_APP)
        if request.url.path.startswith("/api/") or request.url.path.startswith("/results/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    return app


def _warm_worker(coord: Coordinator) -> None:
    try:
        coord.manager.ensure_running()
    except Exception:  # noqa: BLE001 - started on demand later; charge sheets then report the reason
        pass
