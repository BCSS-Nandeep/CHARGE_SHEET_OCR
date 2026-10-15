"""Upload -> classification -> routing through the gateway, for both document types, every file type and the failure paths."""

from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image

from tests.person_images.helpers import make_pdf
from tests.unified_layer.helpers import (CS_TEXT, RED, VAGUE_TEXT, cs_pdf, envelope_errors, ir_docx, make_png, report,
                                         upload, wait_report)


def originals(unified):
    return sorted(p.name for p in unified.settings.originals_dir.iterdir())


def host_documents(unified):
    from app import db as dbmod
    from app.models import DocumentRecord

    with dbmod.session_scope() as db:
        return db.query(DocumentRecord).count()


def image_bytes(fmt):
    buf = io.BytesIO()
    Image.new("RGB", (80, 60), (240, 240, 240)).save(buf, fmt)
    return buf.getvalue()


# --------------------------------------------------------------------------------------------------- routing
def test_ir_docx_is_routed_to_ir_ocr_and_exported(client, unified, tmp_path, fake_faces):
    r = upload(client, "ir_report.docx", ir_docx(tmp_path, [make_png(RED, seed=1)]))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["document_type"] == "interrogation_report" and body["document_type_source"] == "auto"
    doc_id = body["document_id"]
    assert unified.registry.adapter_of(doc_id) == "ir_ocr"
    assert unified.worker.uploads == []                       # the charge-sheet worker never saw it

    rep = wait_report(client, doc_id, ocr_status="succeeded", image_extraction_status="succeeded")
    assert envelope_errors(rep) == []
    assert rep["ocr"]["pipeline"] == "ir_ocr"
    assert rep["ocr"]["payload"]["results"]["schema_version"] == "2.0.0"      # IR_OCR's own schema, untouched
    assert rep["person_images"]["images"]


def test_charge_sheet_is_routed_to_the_worker_and_not_to_ir_ocr(client, unified, tmp_path):
    r = upload(client, "cs.pdf", cs_pdf(tmp_path))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["document_type"] == "charge_sheet" and body["document_type_source"] == "auto"
    doc_id = body["document_id"]
    assert unified.registry.adapter_of(doc_id) == "charge_sheet_ocr"
    assert [u["name"] for u in unified.worker.uploads] == ["cs.pdf"]
    assert host_documents(unified) == 0                       # IR_OCR's database never saw it
    assert client.get(f"/api/v1/documents/{doc_id}").json()["document"]["id"] == doc_id
    assert client.get(f"/api/v1/documents/{doc_id}/results").json()["results"]["schema_version"] == "2.0.0"

    rep = wait_report(client, doc_id, ocr_status="succeeded")
    assert envelope_errors(rep) == []
    assert rep["ocr"]["pipeline"] == "charge_sheet_ocr" and rep["document"]["document_type"] == "charge_sheet"
    assert rep["ocr"]["payload"]["results"]["fields"][0]["label"] == "Accused Name"   # the worker's payload, as is
    assert rep["provenance"]["extractor"]["implementation"] == "CHARGE_SHEET_OCR"


def test_scanned_file_is_classified_from_the_worker_probe(client, unified, tmp_path):
    unified.worker.probe_reply = {
        "text": CS_TEXT, "text_source": "ocr", "text_chars": len(CS_TEXT), "pages_examined": 1, "page_count": 3,
        "ocr_requested": True, "ocr_available": True, "ocr_error": None,
        "detector": {"document_type": "charge_sheet", "confidence": 0.95, "section_hits": [], "uncertain": False, "notes": ""}}
    scan = make_pdf(tmp_path / "scan.pdf", images=[make_png(RED)], text="").read_bytes()
    r = upload(client, "scan.pdf", scan)
    assert r.status_code == 202 and r.json()["document_type"] == "charge_sheet"
    assert unified.worker.probe_calls == 1
    cls = report(client, r.json()["document_id"])["classification"]
    assert cls["text"]["source"] == "ocr" and cls["decision"] == "auto"


def test_original_bytes_are_kept_once_and_shared(client, unified, tmp_path, fake_faces):
    # charge sheet: the kept original and the worker's copy are identical
    data = cs_pdf(tmp_path)
    cs_id = upload(client, "cs.pdf", data).json()["document_id"]
    row = unified.registry.get_document(cs_id)
    assert row["sha256"] == hashlib.sha256(data).hexdigest() and row["size_bytes"] == len(data)
    assert (unified.settings.originals_dir / row["stored_original"]).read_bytes() == data
    assert unified.worker.docs[cs_id]["bytes"] == data
    # interrogation report: the person-image pipeline reads that same kept file
    ir_data = ir_docx(tmp_path, [make_png(RED, seed=3)])
    ir_id = upload(client, "ir.docx", ir_data).json()["document_id"]
    sha = hashlib.sha256(ir_data).hexdigest()
    rep = wait_report(client, ir_id, image_extraction_status="succeeded")
    assert rep["document"]["sha256"] == sha
    from integrations.person_images import store

    assert store.read_manifest(ir_id)["runs"][0]["source_sha256"] == sha


# ---------------------------------------------------------------------------------- unknown / manual type
def test_unknown_document_waits_for_a_choice_and_reaches_no_extractor(client, unified, tmp_path):
    r = upload(client, "memo.pdf", cs_pdf(tmp_path, text=VAGUE_TEXT))
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "awaiting_type" and body["document_id"] is None and body["document_type"] == "unknown"
    assert body["classification"]["reasons"]
    assert unified.worker.uploads == [] and host_documents(unified) == 0
    assert len(originals(unified)) == 1                       # kept so it can be routed later
    pending = client.get("/api/v1/unified/intakes").json()["items"]
    assert [p["intake_id"] for p in pending] == [body["intake_id"]]


def test_waiting_upload_can_be_routed_by_the_user_once(client, unified, tmp_path):
    body = upload(client, "memo.pdf", cs_pdf(tmp_path, text=VAGUE_TEXT)).json()
    r = client.post(f"/api/v1/unified/intakes/{body['intake_id']}/route", json={"document_type": "charge_sheet"})
    assert r.status_code == 202, r.text
    routed = r.json()
    assert routed["document_type"] == "charge_sheet" and routed["document_type_source"] == "manual"
    assert routed["intake_id"] == body["intake_id"]
    assert unified.registry.get_document(routed["document_id"])["type_source"] == "manual"
    assert client.get("/api/v1/unified/intakes").json()["items"] == []
    again = client.post(f"/api/v1/unified/intakes/{body['intake_id']}/route", json={"document_type": "charge_sheet"})
    assert again.status_code == 409                           # never routed twice
    rep = report(client, routed["document_id"])
    assert rep["document"]["document_type_source"] == "manual" and rep["classification"]["decision"] == "manual_selected"


def test_invalid_choice_for_a_waiting_upload_is_refused_and_keeps_it_waiting(client, unified, tmp_path):
    body = upload(client, "memo.pdf", cs_pdf(tmp_path, text=VAGUE_TEXT)).json()
    path = f"/api/v1/unified/intakes/{body['intake_id']}/route"
    assert client.post(path, json={"document_type": "passport"}).status_code == 400
    assert client.post("/api/v1/unified/intakes/00000000-0000-0000-0000-000000000000/route",
                       json={"document_type": "charge_sheet"}).status_code == 404
    assert len(client.get("/api/v1/unified/intakes").json()["items"]) == 1


def test_waiting_upload_can_be_discarded_with_its_stored_copy(client, unified, tmp_path):
    body = upload(client, "memo.pdf", cs_pdf(tmp_path, text=VAGUE_TEXT)).json()
    assert client.delete(f"/api/v1/unified/intakes/{body['intake_id']}").status_code == 204
    assert originals(unified) == [] and client.get("/api/v1/unified/intakes").json()["items"] == []
    assert client.delete(f"/api/v1/unified/intakes/{body['intake_id']}").status_code == 404
    assert unified.worker.uploads == [] and host_documents(unified) == 0


def test_manual_selection_at_upload_overrides_detection_and_says_so(client, unified, tmp_path):
    r = upload(client, "ir_report.docx", ir_docx(tmp_path), document_type="charge_sheet")
    assert r.status_code == 202
    body = r.json()
    assert body["document_type"] == "charge_sheet" and body["document_type_source"] == "manual"
    assert [u["name"] for u in unified.worker.uploads] == ["ir_report.docx"]
    warnings = report(client, body["document_id"])["processing"]["warnings"]
    assert any("looks like a interrogation report" in w["message"] for w in warnings)


def test_manual_selection_skips_the_ocr_probe(client, unified, tmp_path):
    upload(client, "memo.pdf", cs_pdf(tmp_path, text=VAGUE_TEXT), document_type="interrogation_report")
    assert unified.worker.probe_calls == 0


def test_invalid_document_type_value_is_refused(client, unified, tmp_path):
    r = upload(client, "cs.pdf", cs_pdf(tmp_path), document_type="passport")
    assert r.status_code == 400 and originals(unified) == []
    assert upload(client, "cs.pdf", cs_pdf(tmp_path), document_type="auto").status_code == 202   # "auto" is allowed


# ------------------------------------------------------------------------------------------- file types
@pytest.mark.parametrize("fmt,name", [("PNG", "scan.png"), ("JPEG", "scan.jpg"), ("TIFF", "scan.tif")])
def test_image_uploads_go_to_the_charge_sheet_service_when_chosen(client, unified, fmt, name):
    r = upload(client, name, image_bytes(fmt), document_type="charge_sheet")
    assert r.status_code == 202, r.text
    doc_id = r.json()["document_id"]
    assert unified.worker.uploads[-1]["name"] == name
    # charge sheets get no person-image extraction, whatever the file type; OCR is not affected
    rep = wait_report(client, doc_id, ocr_status="succeeded", image_extraction_status="not_applicable")
    assert rep["ocr"]["payload"] is not None


def test_images_are_never_sent_to_ir_ocr_which_does_not_accept_them(client, unified):
    r = upload(client, "scan.png", image_bytes("PNG"), document_type="interrogation_report")
    assert r.status_code == 415 and "does not accept PNG" in r.json()["detail"]
    assert originals(unified) == [] and host_documents(unified) == 0


def test_image_without_readable_text_waits_for_a_choice(client, unified):
    r = upload(client, "scan.png", image_bytes("PNG"))
    assert r.status_code == 202 and r.json()["status"] == "awaiting_type"
    choose_ir = client.post(f"/api/v1/unified/intakes/{r.json()['intake_id']}/route", json={"document_type": "interrogation_report"})
    assert choose_ir.status_code == 415                       # refused ...
    assert len(client.get("/api/v1/unified/intakes").json()["items"]) == 1   # ... and still waiting for another choice


def test_unsupported_and_empty_files_are_refused_and_nothing_is_kept(client, unified):
    r = upload(client, "notes.txt", b"plain text, not a document")
    assert r.status_code == 400 and "accepted types" in r.json()["detail"]
    assert upload(client, "empty.pdf", b"").status_code == 400
    assert upload(client, "old.doc", b"\xd0\xcf\x11\xe0" + b"0" * 100).status_code == 400
    assert originals(unified) == [] and unified.worker.uploads == [] and host_documents(unified) == 0


@pytest.fixture()
def small_uploads(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "1")


def test_oversized_upload_is_refused(small_uploads, client, unified):
    r = upload(client, "big.pdf", b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 10))
    assert r.status_code == 413 and originals(unified) == []


def test_upload_requires_a_file(client):
    assert client.post("/api/v1/documents/upload").status_code == 422
