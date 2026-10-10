import socket
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.logging_utils import redact
from app.security.files import resolve_under, safe_store_filename
from app.security.network_guard import _host_allowed


def test_safe_filename_no_traversal():
    name = safe_store_filename("../../etc/passwd.pdf")
    assert ".." not in name and "/" not in name and "\\" not in name
    assert name.endswith(".pdf")


def test_resolve_under_keeps_files_inside_root(tmp_path: Path):
    p = resolve_under(tmp_path, "../secret.pdf")
    assert p.parent == tmp_path.resolve()
    with pytest.raises(HTTPException):
        resolve_under(tmp_path, "..")


def test_redact_pii():
    out = redact("Call 9876543210 or id 1234 5678 9012 mail a@b.com")
    assert "9876543210" not in out and "a@b.com" not in out and "1234 5678 9012" not in out


@pytest.mark.parametrize(
    "host,allowed",
    [
        ("127.0.0.1", True), ("::1", True), ("localhost", True), ("10.0.0.5", True), ("192.168.1.9", True),
        ("172.16.0.1", True), ("172.31.255.1", True),
        ("172.32.0.1", False),  # outside 172.16.0.0/12 — previously allowed by a '172.' prefix check
        ("8.8.8.8", False), ("example.com", False), ("huggingface.co", False),
    ],
)
def test_host_allowlist(host, allowed):
    assert _host_allowed(host, False) is allowed


def test_network_guard_blocks_connect_and_connect_ex(monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "true")
    monkeypatch.setenv("ALLOW_EXTERNAL_NETWORK", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.security.network_guard import install_network_guard, uninstall_network_guard

    install_network_guard()
    try:
        for method in ("connect", "connect_ex"):
            s = socket.socket()
            try:
                with pytest.raises(OSError, match="blocked"):
                    getattr(s, method)(("93.184.216.34", 80))
            finally:
                s.close()
    finally:
        uninstall_network_guard()


def test_retention_purges_results_corrections_and_file(client, fixtures_dir):
    pdf = fixtures_dir / "sample_01_digital_standard.pdf"
    with pdf.open("rb") as f:
        doc_id = client.post("/api/v1/documents/upload", files={"file": (pdf.name, f, "application/pdf")}).json()["document_id"]
    client.patch(f"/api/v1/documents/{doc_id}/fields",
                 json={"corrections": [{"canonical_name": "district", "corrected_value": "X"}]})

    from app.db import session_scope
    from app.models import CorrectionRecord, DocumentRecord, ResultRecord
    from app.services.retention_service import purge_expired

    with session_scope() as db:
        doc = db.get(DocumentRecord, doc_id)
        doc.created_at = datetime.utcnow() - timedelta(days=400)
        stored = doc.stored_filename
    assert purge_expired() == 1
    with session_scope() as db:
        doc = db.get(DocumentRecord, doc_id)
        assert doc.status == "deleted" and doc.original_filename == "[purged]"
        assert db.query(ResultRecord).filter_by(document_id=doc_id).count() == 0
        assert db.query(CorrectionRecord).filter_by(document_id=doc_id).count() == 0
    from app.config import get_settings

    assert not (Path(get_settings().upload_dir) / stored).exists()
