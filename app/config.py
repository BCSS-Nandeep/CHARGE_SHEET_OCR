"""Application configuration — environment-driven, offline-first defaults."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Charge Sheet Extraction Service"
    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    debug: bool = False

    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'charge_sheets.db'}"

    upload_dir: Path = BASE_DIR / "uploads"
    max_upload_size_mb: int = 25
    retention_days: int = 90

    api_key: str = "change-me-to-a-long-random-secret"
    api_key_header: str = "X-API-Key"
    enable_auth: bool = False
    cors_origins: str = "http://127.0.0.1:8000,http://localhost:8000"

    offline_mode: bool = True
    allow_external_network: bool = False
    disable_telemetry: bool = True
    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_model: str = ""
    llm_allow_external: bool = False

    ocr_enabled: bool = True
    ocr_lang: str = "en"
    ocr_min_text_chars: int = 40
    ocr_dpi: int = 200

    async_processing: bool = True
    processing_timeout_seconds: int = 300
    log_level: str = "INFO"
    log_pii: bool = False

    schema_version: str = "1.0.0"

    @field_validator("upload_dir", mode="before")
    @classmethod
    def _pathify(cls, v: object) -> Path:
        return Path(str(v))

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def ensure_directories(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        (BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
        (BASE_DIR / "logs").mkdir(parents=True, exist_ok=True)

    def llm_is_local_only(self) -> bool:
        if not self.llm_enabled:
            return True
        if self.llm_allow_external:
            return False
        url = (self.llm_base_url or "").lower()
        return url.startswith("http://127.0.0.1") or url.startswith("http://localhost")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
