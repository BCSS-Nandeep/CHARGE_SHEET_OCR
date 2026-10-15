"""The gateway: auth and roles for both document types, key pass-through, path safety, worker outages, history, pages."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ADMIN_KEY, OPERATOR_KEY, REVIEWER_KEY, VIEWER_KEY
from tests.unified_layer.helpers import cs_pdf, ir_docx, report, upload, wait_report


def key(k):
    return {"X-API-Key": k}


@pytest.fixture()
def cs_doc(client, unified, tmp_path):
    doc_id = upload(client, "cs.pdf", cs_pdf(tmp_path)).json()["document_id"]
    wait_report(client, doc_id, ocr_status="succeeded")
    return doc_id


@pytest.fixture()
def ir_doc(client, unified, tmp_path):
    doc_id = upload(client, "ir.docx", ir_docx(tmp_path)).json()["document_id"]
    wait_report(client, doc_id, ocr_status="succeeded")
    return doc_id


@pytest.fixture()
def anonymous(unified):
    with TestClient(unified.gateway) as c:
        yield c


# ------------------------------------------------------------------------------------------- auth + roles
def test_upload_needs_a_key_and_the_operator_role(anonymous, unified, tmp_path):
    data = cs_pdf(tmp_path)
    assert upload(anonymous, "cs.pdf", data).status_code == 401
    assert upload(anonymous, "cs.pdf", data, headers=key(VIEWER_KEY)).status_code == 403
    assert upload(anonymous, "cs.pdf", data, headers=key("wrong-key-wrong-key-123")).status_code == 401
    assert unified.worker.uploads == []
    assert upload(anonymous, "cs.pdf", data, headers=key(OPERATOR_KEY)).status_code == 202


def test_unified_endpoints_all_require_a_key(anonymous, cs_doc, tmp_path):
    for method, path in [
        ("get", f"/api/v1/documents/{cs_doc}/report_result"), ("get", f"/api/v1/documents/{cs_doc}/person-images"),
        ("get", f"/api/v1/unified/documents/{cs_doc}"), ("get", "/api/v1/unified/intakes"),
        ("get", "/api/v1/unified/health"), ("get", "/api/v1/documents"),
        ("post", f"/api/v1/documents/{cs_doc}/person-images/run"),
        ("post", "/api/v1/unified/classify"),
    ]:
        assert getattr(anonymous, method)(path).status_code == 401, path


def test_charge_sheet_calls_follow_the_hosts_roles(anonymous, unified, cs_doc):
    base = f"/api/v1/documents/{cs_doc}"
    patch = {"corrections": [{"field_id": "p1-01", "corrected_value": "X"}]}
    assert anonymous.get(base).status_code == 401
    assert anonymous.get(base, headers=key(VIEWER_KEY)).status_code == 200
    assert anonymous.patch(f"{base}/fields", json=patch, headers=key(VIEWER_KEY)).status_code == 403
    assert anonymous.patch(f"{base}/fields", json=patch, headers=key(OPERATOR_KEY)).status_code == 200
    verify = {"verified": True, "notes": "checked"}
    assert anonymous.post(f"{base}/verify", json=verify, headers=key(OPERATOR_KEY)).status_code == 403
    assert anonymous.post(f"{base}/verify", json=verify, headers=key(REVIEWER_KEY)).status_code == 200
    assert anonymous.delete(base, headers=key(REVIEWER_KEY)).status_code == 403
    assert anonymous.delete(base, headers=key(ADMIN_KEY)).status_code == 204


def test_the_callers_key_is_what_the_worker_receives(anonymous, unified, cs_doc):
    unified.worker.requests.clear()
    anonymous.get(f"/api/v1/documents/{cs_doc}/results", headers=key(VIEWER_KEY))
    anonymous.get(f"/api/v1/documents/{cs_doc}/corrections", headers=key(REVIEWER_KEY))
    seen = [(r["path"].rsplit("/", 1)[-1], r["key"]) for r in unified.worker.requests
            if r["path"].startswith("/api/v1/documents/") and r["method"] == "GET"]
    assert ("results", VIEWER_KEY) in seen and ("corrections", REVIEWER_KEY) in seen


def test_ir_documents_keep_the_hosts_own_role_checks(anonymous, ir_doc):
    base = f"/api/v1/documents/{ir_doc}"
    assert anonymous.get(base).status_code == 401
    assert anonymous.post(f"{base}/verify", json={"verified": True, "notes": "n"}, headers=key(OPERATOR_KEY)).status_code == 403
    assert anonymous.post(f"{base}/verify", json={"verified": True, "notes": "n"}, headers=key(REVIEWER_KEY)).status_code == 200


# ------------------------------------------------------------------------------------------ path safety
@pytest.mark.parametrize("tail", ["../../internal/probe", "%2e%2e/%2e%2e/internal/probe", "..%2f..%2finternal%2fprobe",
                                  "results/../../../internal/probe", "a b", "x;y", "results%00"])
def test_forwarding_rejects_path_tricks(client, unified, cs_doc, tail):
    unified.worker.requests.clear()
    r = client.get(f"/api/v1/documents/{cs_doc}/{tail}")
    assert r.status_code in (404, 422)
    touched = {q["path"] for q in unified.worker.requests}
    assert "/internal/probe" not in touched and not any(".." in p for p in touched), touched


def test_a_query_string_is_forwarded_as_a_query_not_as_a_path(client, unified, cs_doc):
    unified.worker.requests.clear()
    assert client.get(f"/api/v1/documents/{cs_doc}?x=1/../../../internal/probe").status_code == 200
    assert "/internal/probe" not in {q["path"] for q in unified.worker.requests}


def test_unknown_or_malformed_ids_are_not_found(client):
    assert client.get("/api/v1/unified/documents/not-an-id").status_code == 404
    assert client.get("/api/v1/unified/documents/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/api/v1/documents/00000000-0000-0000-0000-000000000000/report_result").status_code == 404
    assert client.get("/cs-static/../../etc/passwd").status_code == 404


# ------------------------------------------------------------------------------------------ worker outage
def test_charge_sheet_worker_outage_is_reported_and_ir_keeps_working(client, unified, cs_doc, ir_doc, tmp_path):
    unified.worker.stop()
    assert client.get(f"/api/v1/documents/{cs_doc}").status_code == 503
    assert client.get(f"/api/v1/documents/{ir_doc}").status_code == 200              # interrogation reports unaffected
    ids = [i["id"] for i in client.get("/api/v1/documents").json()["items"]]
    assert ir_doc in ids                                                             # history degrades, does not fail
    rep = report(client, cs_doc)
    assert rep["processing"]["ocr_status"] == "unknown" and rep["ocr"]["payload"] is None
    assert any("could not be reached" in e["message"] or "unreachable" in e["message"] for e in rep["processing"]["errors"])
    # a new charge sheet is refused (503), nothing half-registered, no copy of the file kept
    kept_before = sorted(p.name for p in unified.settings.originals_dir.iterdir())
    r = upload(client, "cs2.pdf", cs_pdf(tmp_path), document_type="charge_sheet")
    assert r.status_code == 503
    assert sorted(p.name for p in unified.settings.originals_dir.iterdir()) == kept_before
    assert len(unified.registry.list_documents()) == 2
    # an interrogation report can still be uploaded and processed
    assert upload(client, "ir2.docx", ir_docx(tmp_path, name="ir2.docx")).status_code == 202


def test_classification_without_the_worker_falls_back_to_text_and_says_so(client, unified, tmp_path):
    unified.worker.stop()
    r = upload(client, "ir.docx", ir_docx(tmp_path))
    assert r.status_code == 202 and r.json()["document_type"] == "interrogation_report"
    cls = r.json()["classification"]
    assert any("not available" in w for w in cls["warnings"])


# --------------------------------------------------------------------------------------------- history
def test_history_merges_both_services_newest_first(client, unified, cs_doc, ir_doc):
    body = client.get("/api/v1/documents").json()
    by_id = {i["id"]: i for i in body["items"]}
    assert body["total"] == 2 and by_id[cs_doc]["document_type"] == "charge_sheet"
    assert by_id[ir_doc]["document_type"] == "interrogation_report"
    created = [i["created_at"] for i in body["items"]]
    assert created == sorted(created, reverse=True)
    assert [i["id"] for i in client.get("/api/v1/documents?limit=1").json()["items"]] == [body["items"][0]["id"]]
    assert [i["id"] for i in client.get("/api/v1/documents?limit=1&offset=1").json()["items"]] == [body["items"][1]["id"]]
    assert client.get("/api/v1/documents?status=failed").json()["items"] == []
    assert client.get("/api/v1/documents?status=bogus").status_code == 422


# ---------------------------------------------------------------------------------------------- pages
def test_pages_for_both_document_types(client, unified, cs_doc, ir_doc):
    ir_page = client.get(f"/results/{ir_doc}")
    assert ir_page.status_code == 200 and f'data-document-id="{ir_doc}"' in ir_page.text
    assert "/static/js/app.js" in ir_page.text and "unified_images.js" in ir_page.text and "unified.css" in ir_page.text

    cs_page = client.get(f"/results/{cs_doc}")
    assert cs_page.status_code == 200 and f'data-document-id="{cs_doc}"' in cs_page.text
    assert '"/cs-static/js/app.js' in cs_page.text and '"/static/js/app.js' not in cs_page.text   # no clash with IR's app.js
    assert "unified_images.js" in cs_page.text
    assert client.get("/cs-static/js/app.js").text.startswith("/* fake charge-sheet")
    assert client.get("/results/not-an-id").status_code == 404


def test_upload_page_offers_every_file_type_and_a_type_choice(client):
    page = client.get("/")
    assert page.status_code == 200
    for needle in (".pdf", ".docx", ".png", ".jpg", ".tif", 'value="auto"', 'value="charge_sheet"',
                   'value="interrogation_report"', "/unified-static/unified.js"):
        assert needle in page.text
    assert client.get("/unified-static/unified.js").status_code == 200
    assert client.get("/unified-static/unified_images.js").status_code == 200


def test_unified_responses_carry_the_hosts_security_headers(client):
    r = client.get("/")
    assert r.headers["x-frame-options"] == "DENY" and "script-src 'self'" in r.headers["content-security-policy"]
    assert "inline" not in r.headers["content-security-policy"].split("script-src")[1].split(";")[0]


def test_host_routes_still_work_through_the_gateway(client, ir_doc):
    assert client.get("/health").json()["status"] in ("ok", "degraded")
    assert client.get("/history").status_code == 200 and "history-body" in client.get("/history").text
    assert client.get("/static/css/styles.css").status_code == 200
    assert client.get(f"/api/v1/documents/{ir_doc}/raw").status_code in (200, 404)
    assert client.get("/docs").status_code == 200


@pytest.mark.parametrize("path", ["standard", "template.json", "standard.docx", "json", "raw", "corrections", "results"])
def test_ir_endpoints_return_exactly_what_the_unchanged_host_returns(unified, client, ir_doc, path):
    """Includes the optional standard-template (32/33-entry) exports: the gateway adds nothing and removes nothing."""
    url = f"/api/v1/documents/{ir_doc}/{path}"
    with TestClient(unified.host) as direct:
        expected = direct.get(url, headers=key(ADMIN_KEY))
    got = client.get(url)
    assert got.status_code == expected.status_code
    assert got.content == expected.content
    assert got.headers.get("content-type") == expected.headers.get("content-type")
    assert got.headers.get("content-disposition") == expected.headers.get("content-disposition")


def test_blank_standard_template_is_still_served(client):
    r = client.get("/api/v1/standard-ir/template.docx")
    assert r.status_code == 200 and r.content[:2] == b"PK"


# ------------------------------------------------------------------------------------ coordinator view
def test_coordinator_view_reports_both_pipelines(client, unified, cs_doc, ir_doc):
    v = client.get(f"/api/v1/unified/documents/{cs_doc}").json()
    assert v["document_type"] == "charge_sheet" and v["adapter"] == "charge_sheet_ocr"
    assert v["pipelines"]["ocr"]["status"] == "succeeded" and "person_images" in v["pipelines"]
    assert v["review"]["verified"] is False and v["provenance"]["implementation"] == "CHARGE_SHEET_OCR"
    w = client.get(f"/api/v1/unified/documents/{ir_doc}").json()
    assert w["provenance"]["implementation"] == "IR_OCR" and w["pipelines"]["ocr"]["schema_version"] == "2.0.0"
    health = client.get("/api/v1/unified/health").json()
    assert health["charge_sheet_worker"]["healthy"] and set(health["adapters"]) == {"ir_ocr", "charge_sheet_ocr"}
    assert "PDF, DOCX, PNG, JPEG or TIFF" == health["accepted_types"]


def test_classify_endpoint_is_a_dry_run(client, unified, tmp_path):
    r = client.post("/api/v1/unified/classify", files={"file": ("ir.docx", ir_docx(tmp_path), "application/octet-stream")})
    assert r.status_code == 200 and r.json()["document_type"] == "interrogation_report"
    assert unified.worker.uploads == [] and list(unified.settings.originals_dir.iterdir()) == []
    assert unified.registry.list_documents() == []


# ----------------------------------------------------------------------- authentication switched off
def test_without_authentication_only_local_clients_are_served_and_nobody_can_verify(build_unified, tmp_path):
    u = build_unified(enable_auth=False)

    async def remote(scope, receive, send):                 # a request from another machine
        if scope["type"] == "http":
            scope = dict(scope, client=("203.0.113.9", 50000))
        await u.gateway(scope, receive, send)

    with TestClient(remote) as far, TestClient(u.gateway) as near:
        data = cs_pdf(tmp_path)
        assert upload(far, "cs.pdf", data).status_code == 403
        assert far.get("/api/v1/documents").status_code == 403
        assert u.worker.uploads == []
        r = upload(near, "cs.pdf", data)
        assert r.status_code == 202
        doc_id = r.json()["document_id"]
        assert far.get(f"/api/v1/documents/{doc_id}").status_code == 403           # forwarding is not a way around it
        assert near.get(f"/api/v1/documents/{doc_id}").status_code == 200
        verify = near.post(f"/api/v1/documents/{doc_id}/verify", json={"verified": True, "notes": "x"})
        assert verify.status_code == 403                                           # same rule as the standalone services
