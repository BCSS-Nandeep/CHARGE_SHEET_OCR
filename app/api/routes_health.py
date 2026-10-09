"""Health and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app import __version__
from app.config import Settings, get_settings
from app.schemas.api_responses import HealthResponse
from app.services.ocr_service import OcrService, paddleocr_available

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    db_kind = "sqlite" if settings.is_sqlite else "postgresql"
    ocr = OcrService()
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=__version__,
        offline_mode=settings.offline_mode,
        database=db_kind,
        ocr_available=ocr.is_available(),
        llm_enabled=settings.llm_enabled and settings.llm_is_local_only(),
    )
