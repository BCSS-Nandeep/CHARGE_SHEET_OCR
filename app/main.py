"""FastAPI application entrypoint."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__
from app.api import routes_documents, routes_health, routes_results, routes_upload
from app.config import get_settings
from app.db import init_db
from app.logging_utils import get_logger, setup_logging
from app.security.auth import auth_misconfigured
from app.security.network_guard import install_network_guard
from app.services.processing_service import recover_interrupted
from app.services.retention_service import purge_expired

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
# UI pages: only local scripts, no inline script (XSS hardening for personal data on screen)
_CSP_APP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; "
    "connect-src 'self'; frame-src 'self' blob:; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
)
# Swagger / ReDoc pages ship inline bootstrap scripts and load assets from jsdelivr
_CSP_DOCS = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
    "connect-src 'self'; frame-ancestors 'none'; object-src 'none'"
)
_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")


def create_app() -> FastAPI:
    setup_logging()
    settings = get_settings()
    logger = get_logger("app")
    install_network_guard()
    init_db()
    try:
        purge_expired()
    except Exception as exc:
        logger.warning("retention_purge_failed err=%s", type(exc).__name__)
    try:
        recover_interrupted()
    except Exception as exc:
        logger.warning("recover_interrupted_failed err=%s", type(exc).__name__)
    if auth_misconfigured(settings):
        logger.error("ENABLE_AUTH=true but no usable API key is configured; API requests will be refused")
    elif not settings.enable_auth:
        logger.warning("Authentication disabled: serving loopback clients only; verification is unavailable")

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Local-only charge-sheet PDF extraction and data mapping service. "
            "Uploads and extracted data are processed on this host; external AI APIs "
            "are disabled by default."
        ),
        docs_url="/docs" if settings.api_docs_enabled else None,
        redoc_url="/redoc" if settings.api_docs_enabled else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", settings.api_key_header],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        is_docs = path.startswith("/docs") or path.startswith("/redoc")
        response.headers.setdefault("Content-Security-Policy", _CSP_DOCS if is_docs else _CSP_APP)
        if path.startswith("/api/") or path.startswith("/results/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    def asset_version() -> str:
        """Changes whenever app.js / styles.css change, so browsers never keep an old copy."""
        stamps = []
        for rel in ("js/app.js", "css/styles.css"):
            try:
                stamps.append(str(int((BASE_DIR / "static" / rel).stat().st_mtime)))
            except OSError:
                stamps.append("0")
        return "-".join(stamps)

    def page_context(request: Request, **extra):
        return {
            "request": request,
            "api_key_header": settings.api_key_header,
            "app_name": settings.app_name,
            "docs_enabled": settings.api_docs_enabled,
            "asset_version": asset_version(),
            **extra,
        }

    app.include_router(routes_health.router)
    app.include_router(routes_upload.router)
    app.include_router(routes_documents.router)
    app.include_router(routes_results.router)

    static_dir = BASE_DIR / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request):
        return templates.TemplateResponse("index.html", page_context(request))

    @app.get("/results/{document_id}", response_class=HTMLResponse, include_in_schema=False)
    async def results_page(request: Request, document_id: str):
        if not _UUID.match(document_id):
            return HTMLResponse("Not found", status_code=404)
        return templates.TemplateResponse("results.html", page_context(request, document_id=document_id))

    @app.get("/history", response_class=HTMLResponse, include_in_schema=False)
    async def history_page(request: Request):
        return templates.TemplateResponse("history.html", page_context(request))

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.error("unhandled_error path=%s type=%s", request.url.path, type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "detail": "An unexpected error occurred"},
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host=s.app_host,
        port=s.app_port,
        reload=s.debug,
    )
