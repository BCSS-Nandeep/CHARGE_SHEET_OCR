"""The combined report_result.json: native payloads, independent pipelines, regeneration, corrections and reprocessing."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from integrations.person_images import extractor, store
from tests.conftest import ADMIN_KEY, REVIEWER_KEY
from tests.unified_layer.conftest import wait_for
from tests.unified_layer.helpers import (RED, cs_pdf, envelope_errors, ir_docx, make_png, report, upload,
                                         wait_report)


def report_file(unified, doc_id):
    folder = store.find_dir(doc_id)
    return json.loads((folder / "report_result.json").read_text(encoding="utf-8")) if folder else None


def ir_field_id(client, doc_id):
    return client.get(f"/api/v1/documents/{doc_id}/results").json()["results"]["fields"][0]["id"]


# ---------------------------------------------------------------------------------- native payload preserved
def test_ir_payload_in_the_envelope_is_exactly_what_the_service_returns(client, unified, tmp_path, fake_faces):
    doc_id = upload(client, "ir.docx", ir_docx(tmp_path)).json()["document_id"]
    rep = wait_report(client, doc_id, ocr_status="succeeded", image_extraction_status="no_person_images")
    assert rep["ocr"]["payload"] == client.get(f"/api/v1/documents/{doc_id}/results").json()
    assert rep["ocr"]["schema_version"] == rep["ocr"]["payload"]["results"]["schema_version"] == "2.0.0"
    assert rep["provenance"]["native_ocr_schema_version"] == "2.0.0"
    assert rep["provenance"]["envelope_version"] == rep["schema_version"] == "2.0.0"


_VOLATILE = {"created_at", "updated_at", "processed_at", "elapsed_seconds", "extracted_at", "id", "stored_filename",
             "document_id", "previously_seen", "template_id"}


def _normalise(node):
    if isinstance(node, dict):
        return {k: _normalise(v) for k, v in node.items() if k not in _VOLATILE}
    if isinstance(node, list):
        return [_normalise(v) for v in node]
    return node


def test_ir_extraction_through_the_gateway_equals_a_direct_upload_to_the_unchanged_host(client, unified, tmp_path):
    """Same file, same IR_OCR code: going through the unified upload must not change a single extracted value."""
    data = ir_docx(tmp_path)
    via_gateway = upload(client, "ir.docx", data).json()["document_id"]
    with TestClient(unified.host) as direct:
        r = direct.post("/api/v1/documents/upload", headers={"X-API-Key": ADMIN_KEY},
                        files={"file": ("ir.docx", data, "application/octet-stream")})
        direct_id = r.json()["document_id"]
        expected = direct.get(f"/api/v1/documents/{direct_id}/results", headers={"X-API-Key": ADMIN_KEY}).json()
    wait_report(client, via_gateway, ocr_status="succeeded")
    got = client.get(f"/api/v1/documents/{via_gateway}/results").json()
    assert got["results"]["fields"] and _normalise(got["results"]) == _normalise(expected["results"])


def test_charge_sheet_payload_in_the_envelope_is_exactly_what_the_worker_returns(client, unified, tmp_path):
    doc_id = upload(client, "cs.pdf", cs_pdf(tmp_path)).json()["document_id"]
    rep = wait_report(client, doc_id, ocr_status="succeeded")
    assert rep["ocr"]["payload"] == client.get(f"/api/v1/documents/{doc_id}/results").json()
    assert rep["provenance"]["extractor"]["commit"] == "e6d6dd6c1213bd91895f959c98aed51723ebf9a1"


def test_envelope_carries_both_pipelines_review_and_provenance(client, unified, tmp_path, fake_faces):
    doc_id = upload(client, "ir.docx", ir_docx(tmp_path, [make_png(RED)])).json()["document_id"]
    rep = wait_report(client, doc_id, ocr_status="succeeded", image_extraction_status="succeeded")
    assert envelope_errors(rep) == []
    assert rep["document"]["original_filename"] == "ir.docx" and rep["document"]["detected_kind"] == "docx"
    assert rep["review"]["status"] == "needs_review" and rep["review"]["verified"] is False
    assert rep["person_images"]["images"][0]["file"].startswith("person_images/")   # paths are relative
    assert rep["classification"]["decision"] == "auto"


def test_charge_sheets_get_no_person_image_extraction_at_all(client, unified, tmp_path, fake_faces):
    """Person images are for interrogation reports only: no run, no folder of images, and every view says so."""
    doc_id = upload(client, "cs.pdf", cs_pdf(tmp_path, images=[make_png(RED, seed=21)])).json()["document_id"]
    rep = wait_report(client, doc_id, ocr_status="succeeded")
    assert rep["processing"]["image_extraction_status"] == "not_applicable" and rep["person_images"]["images"] == []
    assert rep["person_images"]["status"] == "not_applicable" and envelope_errors(rep) == []
    assert store.read_manifest(doc_id) is None                                # nothing was ever started
    view = client.get(f"/api/v1/documents/{doc_id}/person-images").json()
    assert view["status"] == "not_applicable" and view["images"] == [] and view["runs"] == []
    assert client.post(f"/api/v1/documents/{doc_id}/person-images/run").status_code == 409
    assert client.get(f"/api/v1/documents/{doc_id}/person-images/files/run_20260101T000000Z_abcdef/x.png").status_code == 404
    assert client.get(f"/api/v1/unified/documents/{doc_id}").json()["pipelines"]["person_images"]["status"] == "not_applicable"
    client.post(f"/api/v1/documents/{doc_id}/reprocess")                     # nor does a reprocess start one
    wait_for(lambda: unified.worker.docs[doc_id]["reprocess_count"] == 1)
    assert store.read_manifest(doc_id) is None


def test_download_and_rebuild_from_persisted_state(client, unified, tmp_path):
    doc_id = upload(client, "cs.pdf", cs_pdf(tmp_path)).json()["document_id"]
    wait_report(client, doc_id, ocr_status="succeeded")
    r = client.get(f"/api/v1/documents/{doc_id}/report_result?download=true")
    assert r.headers["content-disposition"] == 'attachment; filename="report_result.json"'
    path = store.find_dir(doc_id) / "report_result.json"
    path.unlink()                                             # a derived file: deleting it loses nothing
    assert report(client, doc_id)["document_id"] == doc_id and path.is_file()


# ------------------------------------------------------------------------------------- independence
def test_ocr_failure_does_not_erase_person_images(client, unified, tmp_path, fake_faces, monkeypatch):
    class Broken:
        def __init__(self, *a, **k):
            pass

        def process(self, *a, **k):
            raise RuntimeError("simulated IR_OCR failure")

    monkeypatch.setattr("app.services.processing_service.ExtractionService", Broken)   # the host's OCR step fails
    doc_id = upload(client, "ir.docx", ir_docx(tmp_path, [make_png(RED, seed=5)])).json()["document_id"]
    rep = wait_report(client, doc_id, ocr_status="failed", image_extraction_status="succeeded")
    assert rep["ocr"]["payload"] is None and rep["processing"]["ocr_error_summary"]
    assert len(rep["person_images"]["images"]) == 1
    assert envelope_errors(rep) == []


def test_image_failure_does_not_invalidate_ocr(client, unified, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(r"detector crashed at C:\secret\folder\model.onnx")

    monkeypatch.setattr(extractor, "save_person_images", boom)
    doc_id = upload(client, "ir.docx", ir_docx(tmp_path)).json()["document_id"]
    rep = wait_report(client, doc_id, ocr_status="succeeded", image_extraction_status="failed")
    assert rep["ocr"]["payload"]["results"]["schema_version"] == "2.0.0"
    err = rep["processing"]["image_extraction_error_summary"]
    assert "detector crashed" in err and "secret" not in err   # file-system paths are scrubbed
    assert rep["person_images"]["images"] == []


def test_person_images_can_be_run_again_without_touching_ocr(client, unified, tmp_path, fake_faces):
    doc_id = upload(client, "ir.docx", ir_docx(tmp_path, [make_png(RED, seed=7)])).json()["document_id"]
    first = wait_report(client, doc_id, ocr_status="succeeded", image_extraction_status="succeeded")
    r = client.post(f"/api/v1/documents/{doc_id}/person-images/run")
    assert r.status_code == 202
    wait_for(lambda: len(store.read_manifest(doc_id)["runs"]) == 2 and
             store.read_manifest(doc_id)["runs"][-1]["status"] == "succeeded")
    again = report(client, doc_id)
    assert again["ocr"]["payload"] == first["ocr"]["payload"]                         # OCR result untouched
    view = client.get(f"/api/v1/documents/{doc_id}/person-images").json()
    assert view["status"] == "succeeded" and len(view["runs"]) == 2 and view["images"][0]["available"]
    image = client.get(view["images"][0]["url"])
    assert image.status_code == 200 and image.headers["content-type"].startswith("image/")


# ----------------------------------------------------------------------------------------- regeneration
def test_export_is_regenerated_when_ocr_completes_without_anyone_asking(client, unified, tmp_path):
    unified.worker.mode = "hold"
    doc_id = upload(client, "cs.pdf", cs_pdf(tmp_path)).json()["document_id"]
    wait_for(lambda: (report_file(unified, doc_id) or {}).get("processing", {}).get("ocr_status") == "processing")
    assert report_file(unified, doc_id)["ocr"]["payload"] is None            # nothing is invented while OCR runs
    assert report_file(unified, doc_id)["processing"]["ocr_progress"]["step"] == "ocr"
    unified.worker.release(doc_id)
    data = wait_for(lambda: (lambda d: d if d["processing"]["ocr_status"] == "succeeded" else None)(report_file(unified, doc_id)))
    assert data["ocr"]["payload"]["results"]["extraction_status"] == "complete"


def test_export_is_regenerated_after_images_finish(client, unified, tmp_path, fake_faces):
    doc_id = upload(client, "ir.docx", ir_docx(tmp_path, [make_png(RED, seed=9)])).json()["document_id"]
    data = wait_for(lambda: (lambda d: d if d and d["processing"]["image_extraction_status"] == "succeeded" else None)(
        report_file(unified, doc_id)))
    assert len(data["person_images"]["images"]) == 1                          # written to disk without anyone asking


@pytest.mark.parametrize("kind", ["ir", "cs"])
def test_saved_corrections_and_verification_reach_the_export(client, unified, tmp_path, kind):
    data, name = (ir_docx(tmp_path), "ir.docx") if kind == "ir" else (cs_pdf(tmp_path), "cs.pdf")
    doc_id = upload(client, name, data).json()["document_id"]
    wait_report(client, doc_id, ocr_status="succeeded")
    fid = ir_field_id(client, doc_id) if kind == "ir" else "p1-01"
    r = client.patch(f"/api/v1/documents/{doc_id}/fields",
                     json={"corrections": [{"field_id": fid, "corrected_value": "Line one\nLine two  ", "review_status": "corrected"}]})
    assert r.status_code == 200, r.text
    rep = wait_for(lambda: (lambda d: d if d["review"]["corrected_fields"] == 1 else None)(report(client, doc_id)))
    corrected = rep["ocr"]["payload"]["field_corrections"][fid]
    assert corrected["corrected_value"] == "Line one\nLine two  "            # multiline text and spacing kept as typed
    # the file on disk follows too (written by the follow-up, not just by the GET above)
    wait_for(lambda: (report_file(unified, doc_id) or {}).get("review", {}).get("corrected_fields") == 1)

    v = client.post(f"/api/v1/documents/{doc_id}/verify", json={"verified": True, "notes": "checked"},
                    headers={"X-API-Key": REVIEWER_KEY})
    assert v.status_code == 200, v.text
    wait_for(lambda: (report_file(unified, doc_id) or {}).get("review", {}).get("status") == "verified")
    assert report_file(unified, doc_id)["review"]["verified"] is True
    assert envelope_errors(report_file(unified, doc_id)) == []


def test_reprocess_starts_a_new_image_run_and_refreshes_the_export(client, unified, tmp_path, fake_faces):
    doc_id = upload(client, "ir.docx", ir_docx(tmp_path, [make_png(RED, seed=11)])).json()["document_id"]
    wait_report(client, doc_id, ocr_status="succeeded", image_extraction_status="succeeded")
    r = client.post(f"/api/v1/documents/{doc_id}/reprocess")
    assert r.status_code in (200, 202), r.text
    wait_for(lambda: len(store.read_manifest(doc_id)["runs"]) == 2 and
             store.read_manifest(doc_id)["runs"][-1]["status"] == "succeeded")
    rep = wait_report(client, doc_id, ocr_status="succeeded", image_extraction_status="succeeded")
    assert rep["person_images"]["latest_run_id"] == store.read_manifest(doc_id)["runs"][-1]["run_id"]


def test_charge_sheet_reprocess_refreshes_the_export(client, unified, tmp_path):
    doc_id = upload(client, "cs.pdf", cs_pdf(tmp_path)).json()["document_id"]
    wait_report(client, doc_id, ocr_status="succeeded")
    unified.worker.mode = "hold"
    assert client.post(f"/api/v1/documents/{doc_id}/reprocess").status_code in (200, 202)
    wait_for(lambda: (report_file(unified, doc_id) or {}).get("processing", {}).get("ocr_status") == "processing")
    unified.worker.release(doc_id)
    wait_for(lambda: (report_file(unified, doc_id) or {}).get("processing", {}).get("ocr_status") == "succeeded")


def test_deleted_document_disappears_from_the_unified_views(client, unified, tmp_path):
    doc_id = upload(client, "cs.pdf", cs_pdf(tmp_path)).json()["document_id"]
    wait_report(client, doc_id, ocr_status="succeeded")
    kept = unified.settings.originals_dir / unified.registry.get_document(doc_id)["stored_original"]
    assert kept.is_file()
    assert client.delete(f"/api/v1/documents/{doc_id}").status_code == 204
    wait_for(lambda: unified.registry.get_document(doc_id)["deleted"])
    wait_for(lambda: not kept.exists())                       # the layer's own copy of the original goes with it
    assert client.get(f"/api/v1/documents/{doc_id}/report_result").status_code == 404
    assert client.get(f"/api/v1/documents/{doc_id}").status_code == 404
    assert doc_id not in [i["id"] for i in client.get("/api/v1/documents").json()["items"]]


# --------------------------------------------------------------------------------------- legacy documents
def test_document_uploaded_to_ir_ocr_before_the_merge_still_works(client, unified, tmp_path, fake_faces):
    """Not in the registry (uploaded straight to the host): owned by IR_OCR, nothing is migrated or rewritten."""
    with TestClient(unified.host) as direct:
        r = direct.post("/api/v1/documents/upload", headers={"X-API-Key": ADMIN_KEY},
                        files={"file": ("old.docx", ir_docx(tmp_path, [make_png(RED, seed=13)]), "application/octet-stream")})
    doc_id = r.json()["document_id"]
    assert unified.registry.get_document(doc_id) is None
    rep = report(client, doc_id)
    assert rep["document"]["document_type"] == "interrogation_report" and rep["document"]["document_type_source"] == "legacy"
    assert rep["classification"] is None and envelope_errors(rep) == []
    assert doc_id in [i["id"] for i in client.get("/api/v1/documents").json()["items"]]
    client.post(f"/api/v1/documents/{doc_id}/person-images/run")
    wait_for(lambda: store.read_manifest(doc_id) and store.read_manifest(doc_id)["runs"][-1]["status"] == "succeeded")
    assert report(client, doc_id)["person_images"]["images"]
