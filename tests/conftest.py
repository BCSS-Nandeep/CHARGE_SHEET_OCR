"""Pytest fixtures — isolated temp DB and upload dir, deterministic settings."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_NAMES = [
    "sample_01_digital_standard.pdf",
    "sample_02_alternate_labels.pdf",
    "sample_03_multipage.pdf",
    "sample_04_scanned_image.pdf",
    "sample_05_header_table_narrative_refs.pdf",
    "sample_06_missing_fields_and_traps.pdf",
]

ADMIN_KEY = "test-admin-key-0123456789"
OPERATOR_KEY = "test-operator-key-0123456789"
REVIEWER_KEY = "test-reviewer-key-0123456789"
VIEWER_KEY = "test-viewer-key-0123456789"

# Deterministic defaults for every test (a developer's .env must not leak in).
# OCR is slow (~1 min/page on CPU); OCR tests opt in with RUN_OCR_TESTS=1.
os.environ.setdefault("OCR_ENABLED", "false")
os.environ["ASYNC_PROCESSING"] = "false"
os.environ["OFFLINE_MODE"] = "true"
os.environ["ALLOW_EXTERNAL_NETWORK"] = "false"
# The VLM path is opt-in per test: a developer's .env (live local model) must not leak in.
os.environ["IR_STANDARD_ENABLED"] = "false"
os.environ["LLM_ENABLED"] = "false"


@pytest.fixture(scope="session", autouse=True)
def _prepare_samples():
    """Generate synthetic fixtures only when missing — never rewrite tracked files."""
    if all((FIXTURES / n).exists() for n in SAMPLE_NAMES):
        return
    import importlib.util
    import sys

    script = ROOT / "scripts" / "generate_sample_pdfs.py"
    spec = importlib.util.spec_from_file_location("generate_sample_pdfs", script)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["generate_sample_pdfs"] = mod
    spec.loader.exec_module(mod)
    mod.main(only_missing=True)
    assert all((FIXTURES / n).exists() for n in SAMPLE_NAMES), "sample PDFs missing"


@pytest.fixture(autouse=True)
def _fresh_settings():
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_client(tmp_path, monkeypatch, *, enable_auth: bool, client_host: str = "testclient"):
    db_path = tmp_path / "test.db"
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setenv("API_KEY", ADMIN_KEY)
    monkeypatch.setenv(
        "API_KEYS",
        f"ops:{OPERATOR_KEY}:operator;rita:{REVIEWER_KEY}:reviewer;vic:{VIEWER_KEY}:viewer",
    )
    monkeypatch.setenv("ENABLE_AUTH", "true" if enable_auth else "false")
    monkeypatch.setenv("CORS_ORIGINS", "http://testserver")

    from app.config import get_settings

    get_settings.cache_clear()

    import app.db as dbmod
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    settings = get_settings()
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    dbmod.engine = engine
    dbmod.SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    import app.models  # noqa: F401
    from app.db import Base

    Base.metadata.create_all(bind=engine)

    from app.main import create_app

    app = create_app()
    if client_host == "testclient":
        return TestClient(app)

    async def with_client_host(scope, receive, send):
        # Simulate a request from another machine (TestClient always uses "testclient")
        if scope["type"] in ("http", "websocket"):
            scope = dict(scope, client=(client_host, 50000))
        await app(scope, receive, send)

    return TestClient(with_client_host)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch, enable_auth=True) as c:
        c.headers.update({"X-API-Key": ADMIN_KEY})
        yield c


@pytest.fixture()
def make_client(tmp_path, monkeypatch):
    """Factory for clients with custom auth settings / client address."""
    def factory(**kwargs):
        return _make_client(tmp_path, monkeypatch, **kwargs)

    return factory


@pytest.fixture()
def fixtures_dir() -> Path:
    return FIXTURES
