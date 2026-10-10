"""REST API contracts, authorization and workflow consistency."""

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import ADMIN_KEY, OPERATOR_KEY, REVIEWER_KEY, VIEWER_KEY


def _upload(client: TestClient, pdf: Path, key: str = ADMIN_KEY):
    with pdf.open("rb") as f:
        res = client.post(
            "/api/v1/documents/upload",
            files={"file": (pdf.name, f, "application/pdf")},
            headers={"X-API-Key": key},
        )
    assert res.status_code == 202, res.text
    return res.json()["document_id"]


def test_health_reports_ocr_and_auth(client: TestClient):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["offline_mode"] is True
    assert body["auth_enabled"] is True and body["auth_misconfigured"] is False
    assert "ocr_engine" in body


def test_security_headers(client: TestClient):
    res = client.get("/api/v1/documents")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in res.headers["Content-Security-Policy"]


def test_missing_or_wrong_key_rejected(client: TestClient, fixtures_dir: Path):
    pdf = fixtures_dir / "sample_01_digital_standard.pdf"
    with pdf.open("rb") as f:
        res = client.post("/api/v1/documents/upload", files={"file": (pdf.name, f, "application/pdf")},
                          headers={"X-API-Key": ""})
    assert res.status_code == 401
    assert client.get("/api/v1/documents", headers={"X-API-Key": "x" * 30}).status_code == 401


def test_roles_are_enforced(client: TestClient, fixtures_dir: Path):
    pdf = fixtures_dir / "sample_01_digital_standard.pdf"
    with pdf.open("rb") as f:
        res = client.post("/api/v1/documents/upload", files={"file": (pdf.name, f, "application/pdf")},
                          headers={"X-API-Key": VIEWER_KEY})
    assert res.status_code == 403
    doc_id = _upload(client, pdf, OPERATOR_KEY)
    assert client.get(f"/api/v1/documents/{doc_id}/results", headers={"X-API-Key": VIEWER_KEY}).status_code == 200
    res = client.post(f"/api/v1/documents/{doc_id}/verify", json={"verified": True, "notes": "ok"},
                      headers={"X-API-Key": OPERATOR_KEY})
    assert res.status_code == 403
    assert client.delete(f"/api/v1/documents/{doc_id}", headers={"X-API-Key": REVIEWER_KEY}).status_code == 403


def test_correct_verify_reprocess_workflow(client: TestClient, fixtures_dir: Path):
    doc_id = _upload(client, fixtures_dir / "sample_01_digital_standard.pdf", OPERATOR_KEY)
    meta = client.get(f"/api/v1/documents/{doc_id}").json()["document"]
    assert meta["status"] == "completed"
    payload = client.get(f"/api/v1/documents/{doc_id}/results").json()
    assert payload["results"]["extraction_status"] == "complete"
    assert payload["results"]["case"]["district"]["original_value"] == "Sample District"

    bad = client.patch(f"/api/v1/documents/{doc_id}/fields",
                       json={"corrections": [{"canonical_name": "no_such_field", "corrected_value": "x"}]})
    assert bad.status_code == 400

    patch = client.patch(
        f"/api/v1/documents/{doc_id}/fields",
        json={"corrections": [{"canonical_name": "district", "corrected_value": "Corrected District"}]},
        headers={"X-API-Key": OPERATOR_KEY},
    )
    assert patch.status_code == 200
    district = patch.json()["results"]["case"]["district"]
    assert district["original_value"] == "Sample District"  # original preserved
    assert district["corrected_value"] == "Corrected District"
    corr = patch.json()["field_corrections"]["district"]
    assert corr["reviewer"] == "ops" and corr["original_value"] == "Sample District"

    assert client.post(f"/api/v1/documents/{doc_id}/verify", json={"verified": True},
                       headers={"X-API-Key": REVIEWER_KEY}).status_code == 400  # notes required
    verify = client.post(f"/api/v1/documents/{doc_id}/verify", json={"verified": True, "notes": "Checked against PDF"},
                         headers={"X-API-Key": REVIEWER_KEY})
    assert verify.status_code == 200
    assert verify.json()["document"]["verified"] is True
    assert verify.json()["document"]["verified_by"] == "rita"

    # Reprocessing creates a new, unverified extraction; the unchanged-source correction is re-applied
    re = client.post(f"/api/v1/documents/{doc_id}/reprocess", headers={"X-API-Key": OPERATOR_KEY})
    assert re.status_code == 200
    meta = client.get(f"/api/v1/documents/{doc_id}").json()["document"]
    assert meta["status"] == "completed" and meta["verified"] is False
    district = client.get(f"/api/v1/documents/{doc_id}/results").json()["results"]["case"]["district"]
    assert district["corrected_value"] == "Corrected District"

    history = client.get(f"/api/v1/documents/{doc_id}/corrections").json()
    assert [h["canonical_name"] for h in history] == ["district", "_verification"]

    assert client.get(f"/api/v1/documents/{doc_id}/source").headers["content-type"].startswith("application/pdf")


def test_failed_extraction_is_not_presented_as_results(client: TestClient, fixtures_dir: Path):
    doc_id = _upload(client, fixtures_dir / "sample_04_scanned_image.pdf")
    meta = client.get(f"/api/v1/documents/{doc_id}").json()["document"]
    assert meta["status"] == "failed"
    assert "OCR" in meta["error_message"]
    res = client.get(f"/api/v1/documents/{doc_id}/results")
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail["extraction_status"] == "failed"
    assert any(s["stage"] == "ocr_local" for s in detail["stage_results"])
    assert "case" not in detail  # diagnostics only, no extracted values
    assert client.patch(f"/api/v1/documents/{doc_id}/fields",
                        json={"corrections": [{"canonical_name": "district", "corrected_value": "x"}]}).status_code == 409


def test_delete_purges_content(client: TestClient, fixtures_dir: Path):
    doc_id = _upload(client, fixtures_dir / "sample_02_alternate_labels.pdf")
    assert client.delete(f"/api/v1/documents/{doc_id}").status_code == 204
    assert client.get(f"/api/v1/documents/{doc_id}").status_code == 404
    assert client.get(f"/api/v1/documents/{doc_id}/results").status_code == 404
    from app.config import get_settings

    assert not list(Path(get_settings().upload_dir).glob("*alternate_labels*"))


def test_reject_non_pdf_and_oversized(client: TestClient, monkeypatch):
    res = client.post("/api/v1/documents/upload", files={"file": ("note.txt", b"hello", "text/plain")})
    assert res.status_code == 400
    monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "1")
    from app.config import get_settings

    get_settings.cache_clear()
    big = b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 10)
    res = client.post("/api/v1/documents/upload", files={"file": ("big.pdf", big, "application/pdf")})
    assert res.status_code == 413


def test_auth_disabled_serves_loopback_only_and_forbids_verify(make_client, fixtures_dir: Path):
    with make_client(enable_auth=False) as local:
        doc_id = _upload(local, fixtures_dir / "sample_01_digital_standard.pdf", key="")
        res = local.post(f"/api/v1/documents/{doc_id}/verify", json={"verified": True, "notes": "x"})
        assert res.status_code == 403 and "authenticated" in res.json()["detail"]
    with make_client(enable_auth=False, client_host="10.1.2.3") as remote:
        assert remote.get("/api/v1/documents").status_code == 403


def test_placeholder_key_is_refused(make_client, monkeypatch):
    monkeypatch.setenv("API_KEYS", "")
    with make_client(enable_auth=True) as c:
        monkeypatch.setenv("API_KEY", "change-me-to-a-long-random-secret")
        monkeypatch.setenv("API_KEYS", "")
        from app.config import get_settings

        get_settings.cache_clear()
        res = c.get("/api/v1/documents", headers={"X-API-Key": "change-me-to-a-long-random-secret"})
        assert res.status_code == 503


def test_api_key_header_name_is_configurable(make_client, monkeypatch):
    monkeypatch.setenv("API_KEY_HEADER", "X-Service-Token")
    with make_client(enable_auth=True) as c:
        assert c.get("/api/v1/documents", headers={"X-Service-Token": ADMIN_KEY}).status_code == 200
        assert c.get("/api/v1/documents", headers={"X-API-Key": ADMIN_KEY}).status_code == 401


def test_all_samples_reach_a_terminal_state(client: TestClient, fixtures_dir: Path):
    for pdf in sorted(fixtures_dir.glob("sample_*.pdf")):
        doc_id = _upload(client, pdf)
        status = client.get(f"/api/v1/documents/{doc_id}").json()["document"]["status"]
        expected = "failed" if "scanned" in pdf.name else "completed"
        assert status == expected, pdf.name
