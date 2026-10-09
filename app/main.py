"""FastAPI application entrypoint."""

from __future__ import annotations

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
from app.security.network_guard import install_network_guard
from app.services.retention_service import purge_expired

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def create_app() -> FastAPI:
    setup_logging()
    settings = get_settings()
    install_network_guard()
    init_db()
    try:
        purge_expired()
    except Exception:
        pass

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Local-only charge-sheet PDF extraction and data mapping service. "
            "Uploads and extracted data are processed on this host; external AI APIs "
            "are disabled by default."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(routes_health.router)
    app.include_router(routes_upload.router)
    app.include_router(routes_documents.router)
    app.include_router(routes_results.router)

    static_dir = BASE_DIR / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request):
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "api_key_header": settings.api_key_header,
                "app_name": settings.app_name,
            },
        )

    @app.get("/results/{document_id}", response_class=HTMLResponse, include_in_schema=False)
    async def results_page(request: Request, document_id: str):
        return templates.TemplateResponse(
            "results.html",
            {
                "request": request,
                "document_id": document_id,
                "api_key_header": settings.api_key_header,
                "app_name": settings.app_name,
            },
        )

    @app.get("/history", response_class=HTMLResponse, include_in_schema=False)
    async def history_page(request: Request):
        return templates.TemplateResponse(
            "history.html",
            {
                "request": request,
                "api_key_header": settings.api_key_header,
                "app_name": settings.app_name,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger = get_logger("app")
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
