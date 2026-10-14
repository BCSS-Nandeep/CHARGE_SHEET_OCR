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
    max_pages: int = 200
    retention_days: int = 90

    api_key: str = "change-me-to-a-long-random-secret"
    # Optional named keys with roles: "alice:<key>:reviewer;ops:<key>:operator".
    # Roles: viewer < operator < reviewer < admin. API_KEY maps to user "default" (admin).
    api_keys: str = ""
    api_key_header: str = "X-API-Key"
    enable_auth: bool = False
    cors_origins: str = "http://127.0.0.1:8000,http://localhost:8000"

    offline_mode: bool = True
    allow_external_network: bool = False
    # Hostnames the offline guard may connect to (e.g. a private Postgres host)
    network_allowlist: str = ""
    disable_telemetry: bool = True
    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_model: str = ""
    llm_allow_external: bool = False

    ocr_enabled: bool = True
    ocr_lang: str = "en"
    ocr_min_text_chars: int = 40
    ocr_dpi: int = 200
    # Paddle 3.x oneDNN kernels crash on some CPUs/models
    # ("ConvertPirAttribute2RuntimeAttribute not support"); off by default.
    ocr_enable_mkldnn: bool = False
    # Measured on an i7-14700: 8 threads is faster than the default 10 (more threads
    # spill onto efficiency cores), and text detection on a 1600 px image is ~2x
    # faster than full resolution and finds lines full resolution misses.
    ocr_cpu_threads: int = 8
    ocr_det_limit_side_len: int = 1600  # 0 = detect at full rendered resolution
    # Paddle's default 0.6 drops faint lines on photocopies (court headings, the
    # first narrative lines); 0.3 recovers them at ~10% extra detection time.
    ocr_det_box_thresh: float = 0.3  # 0 = engine default
    # Explicit path to tesseract.exe when it is not on PATH.
    tesseract_cmd: str = ""

    # Hybrid stack (PyMuPDF router → Paddle/PP-Structure → Docling → VL).
    # Docling/VL are opt-in: they pull large models and are slow offline until cached.
    ppstructure_enabled: bool = True
    docling_enabled: bool = False
    vl_enabled: bool = False
    vl_require_gpu: bool = True
    vl_pipeline: str = "PaddleOCR-VL"
    ocr_accept_min_confidence: float = 0.70
    # Paddle angle/doc orientation classifiers (extra models; enable when scans are rotated)
    ocr_use_orientation: bool = False
    # Persist per-page native/OCR text into results JSON (sensitive). Default off.
    store_page_text: bool = False

    # Local vision-language model (OpenAI-compatible server, e.g. llama.cpp serving
    # Nanonets-OCR2-3B). Disabled unless VLM_ENABLED=true; the URL and model name are
    # read from the environment and never defaulted, so nothing is called by accident.
    vlm_enabled: bool = False
    vlm_base_url: str = ""  # e.g. http://127.0.0.1:8081/v1
    vlm_model: str = ""
    vlm_api_key: str = ""  # sent as a Bearer token when set; never logged
    # Who turns scanned/garbled pages into text: "nanonets" (the VLM reads the page image)
    # or "paddleocr" (local OCR; the VLM then only maps fields from that text).
    vlm_transcribe_engine: str = "nanonets"
    # Engine used for a page when the VLM transcription of it fails: "none" (the page
    # is reported failed) or "paddleocr". Never switched to silently.
    vlm_fallback_engine: str = "none"
    vlm_timeout_seconds: int = 600  # per request (CPU inference is slow)
    vlm_connect_timeout_seconds: int = 5
    vlm_max_retries: int = 1  # extra attempts for timeouts / connection errors / 5xx only
    vlm_retry_backoff_seconds: float = 5.0
    vlm_max_image_side: int = 1536  # longest side, pixels, of a page image sent to the model
    vlm_transcribe_max_tokens: int = 3072
    # Mapping answers are short; a longer one is a runaway on a shared single-slot server
    vlm_map_max_tokens: int = 1500
    vlm_map_chunk_chars: int = 9000  # page text per mapping request (server context is small)
    # A page with more text than this is asked one section per request (the model fills every field
    # it is shown, so one big answer hits the token limit on a dense page)
    vlm_map_split_chars: int = 1800
    # ...and a list section (accused, witnesses, ...) of such a page is asked in pieces of at most
    # this many characters: its answer grows with the number of people listed
    vlm_map_list_chunk_chars: int = 1100
    vlm_min_page_chars: int = 20  # pages with less text are not sent for mapping
    # Consecutive connection failures after which the remaining pages are skipped
    vlm_abort_after_failures: int = 2
    # Time budget for a whole document on the VLM path (0 = unlimited); pages past it are
    # skipped and the document is reported partial.
    vlm_document_budget_seconds: int = 7200
    # Wait for the shared server's single slot to be free before each request (read-only
    # /slots check; 0 = send immediately and let the server queue the request).
    vlm_wait_idle_seconds: int = 0

    # /docs and /redoc make the *browser* load Swagger/ReDoc assets from cdn.jsdelivr.net.
    # Disable on air-gapped networks.
    api_docs_enabled: bool = True

    async_processing: bool = True
    # OCR on CPU takes ~1 min/page; past this budget remaining pages are skipped (partial)
    processing_timeout_seconds: int = 900
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
