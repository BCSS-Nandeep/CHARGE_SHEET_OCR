"""Health and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app import __version__
from app.config import Settings, get_settings
from app.schemas.api_responses import HealthResponse
from app.security.auth import auth_misconfigured
from app.services.docling_service import DoclingService
from app.services.ocr_service import OcrService
from app.services.structure_service import StructureService
from app.services.vl_service import VlService
from app.services.vlm_client import VlmClient

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    db_kind = "sqlite" if settings.is_sqlite else "postgresql"
    ocr = OcrService()
    ocr_status = ocr.status()
    misconfigured = auth_misconfigured(settings)
    vlm = VlmClient(settings).health() if settings.vlm_enabled else None
    return HealthResponse(
        status="degraded" if misconfigured else "ok",
        service=settings.app_name,
        version=__version__,
        offline_mode=settings.offline_mode,
        database=db_kind,
        ocr_available=ocr.is_available(),
        ocr_engine=ocr_status["engine"],
        ocr_error=ocr_status["paddleocr_error"],
        ppstructure_available=StructureService().is_available(),
        docling_available=DoclingService().is_available(),
        vl_available=VlService().is_available(),
        vlm_enabled=settings.vlm_enabled,
        vlm_available=bool(vlm and vlm["reachable"] and vlm["model_available"]),
        vlm_model=settings.vlm_model or None,
        vlm_transcribe_engine=settings.vlm_transcribe_engine if settings.vlm_enabled else None,
        vlm_error=vlm["error"] if vlm else None,
        store_page_text=settings.store_page_text,
        auth_enabled=settings.enable_auth,
        auth_misconfigured=misconfigured,
        llm_enabled=settings.llm_enabled and settings.llm_is_local_only(),
    )
