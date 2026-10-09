import time
from pathlib import Path

from fastapi.testclient import TestClient


def _upload(client: TestClient, pdf: Path):
    with pdf.open("rb") as f:
        res = client.post(
            "/api/v1/documents/upload",
            files={"file": (pdf.name, f, "application/pdf")},
        )
    assert res.status_code == 202, res.text
    return res.json()


def test_health(client: TestClient):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["offline_mode"] is True


def test_auth_required(client: TestClient, fixtures_dir: Path):
    pdf = fixtures_dir / "sample_01_digital_standard.pdf"
    # remove auth header
    bad = TestClient(client.app)
    with pdf.open("rb") as f:
        res = bad.post(
            "/api/v1/documents/upload",
            files={"file": (pdf.name, f, "application/pdf")},
        )
    assert res.status_code == 401


def test_upload_process_results_correct_verify(client: TestClient, fixtures_dir: Path):
    pdf = fixtures_dir / "sample_01_digital_standard.pdf"
    accepted = _upload(client, pdf)
    doc_id = accepted["document_id"]

    # Synchronous processing in tests
    meta = client.get(f"/api/v1/documents/{doc_id}")
    assert meta.status_code == 200
    status = meta.json()["document"]["status"]
    assert status in ("completed", "queued", "processing", "uploaded")
    # If somehow still processing, wait briefly
    for _ in range(20):
        status = client.get(f"/api/v1/documents/{doc_id}").json()["document"]["status"]
        if status in ("completed", "failed", "verified"):
            break
        time.sleep(0.1)
    assert status == "completed", client.get(f"/api/v1/documents/{doc_id}").json()

    results = client.get(f"/api/v1/documents/{doc_id}/results")
    assert results.status_code == 200
    payload = results.json()
    assert payload["results"]["case"]["district"] is not None

    patch = client.patch(
        f"/api/v1/documents/{doc_id}/fields",
        json={
            "corrections": [
                {
                    "canonical_name": "district",
                    "corrected_value": "Corrected District",
                    "review_status": "corrected",
                }
            ]
        },
    )
    assert patch.status_code == 200
    # Original preserved
    district = patch.json()["results"]["case"]["district"]
    assert district["original_value"]
    assert district["corrected_value"] == "Corrected District"

    verify = client.post(
        f"/api/v1/documents/{doc_id}/verify",
        json={"verified": True, "notes": "Reviewed in automated test"},
    )
    assert verify.status_code == 200
    assert verify.json()["document"]["verified"] is True

    listing = client.get("/api/v1/documents")
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1

    source = client.get(f"/api/v1/documents/{doc_id}/source")
    assert source.status_code == 200
    assert source.headers["content-type"].startswith("application/pdf")


def test_reject_non_pdf(client: TestClient):
    res = client.post(
        "/api/v1/documents/upload",
        files={"file": ("note.txt", b"hello", "text/plain")},
    )
    assert res.status_code in (400, 422)


def test_all_four_samples(client: TestClient, fixtures_dir: Path):
    pdfs = sorted(fixtures_dir.glob("sample_*.pdf"))
    assert len(pdfs) == 4
    for pdf in pdfs:
        accepted = _upload(client, pdf)
        doc_id = accepted["document_id"]
        status = client.get(f"/api/v1/documents/{doc_id}").json()["document"]["status"]
        # scanned without OCR may complete with warnings, or fail only on hard errors
        assert status in ("completed", "failed")
        if status == "completed":
            res = client.get(f"/api/v1/documents/{doc_id}/results")
            assert res.status_code == 200
