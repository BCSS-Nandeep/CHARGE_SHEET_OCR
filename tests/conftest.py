"""Pytest fixtures — isolated temp DB and upload dir."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Configure env before importing app
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _prepare_samples():
    import importlib.util
    import sys

    script = ROOT / "scripts" / "generate_sample_pdfs.py"
    spec = importlib.util.spec_from_file_location("generate_sample_pdfs", script)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["generate_sample_pdfs"] = mod
    spec.loader.exec_module(mod)
    mod.main()
    assert list(FIXTURES.glob("*.pdf")), "sample PDFs missing"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("ENABLE_AUTH", "true")
    monkeypatch.setenv("ASYNC_PROCESSING", "false")
    monkeypatch.setenv("OCR_ENABLED", "false")
    monkeypatch.setenv("OFFLINE_MODE", "true")
    monkeypatch.setenv("ALLOW_EXTERNAL_NETWORK", "false")
    monkeypatch.setenv("CORS_ORIGINS", "http://testserver")

    # Reset settings cache and engine
    from app.config import get_settings

    get_settings.cache_clear()

    import app.db as dbmod
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
    )
    dbmod.engine = engine
    dbmod.SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    from app.db import Base, init_db
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        c.headers.update({"X-API-Key": "test-api-key"})
        yield c

    get_settings.cache_clear()


@pytest.fixture()
def fixtures_dir() -> Path:
    return FIXTURES
