"""One upload -> OCR + person images + combined report_result.json, with independent failure handling.

Run through the real OCR application (existing fixtures, real OCR code path for DOCX / text PDFs) with the integration
attached from outside. Only the face decision is replaced (see conftest); everything else is the real code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("cv2")

from integrations.person_images import extractor, report_result, service, store  # noqa: E402
from tests.conftest import ADMIN_KEY, OPERATOR_KEY, VIEWER_KEY  # noqa: E402
from urllib.parse import quote  # noqa: E402

from tests.person_images.helpers import (  # noqa: E402
    BLUE, RED, doc_folder, make_docx, make_pdf, make_png, report_file, upload, wait_for, wait_images,
)

pytestmark = pytest.mark.usefixtures("fake_faces")


def _upload_ok(client, path) -> str:
    r = upload(client, path)
    assert r.status_code == 202, r.text
    return r.json()["document_id"]


def _two_people_one_other(tmp_path):
    return make_docx(tmp_path / "report.docx", [make_png(RED, seed=1), make_png(BLUE, seed=2), make_png(RED, seed=3)])


# ---------------------------------------------------------------------------------------------------------
# 1. both succeed: one upload -> everything, and the combined JSON preserves the OCR payload exactly
# ---------------------------------------------------------------------------------------------------------

def test_one_upload_gives_ocr_person_images_and_the_combined_json(integrated, out_root, tmp_path):
    client = integrated
    doc_id = _upload_ok(client, _two_people_one_other(tmp_path))
    view = wait_images(client, doc_id)
    assert view["status"] == "succeeded" and len(view["images"]) == 2 and view["total_images_found"] == 3
    assert view["skipped_images"] == 1 and view["unreadable_images"] == 0
    # the folder is named after the uploaded file, the images after it: "<file name> image", "<file name> image (2)"
    assert [im["name"] for im in view["images"]] == ["report image.png", "report image (2).png"]
    assert doc_folder(doc_id) == out_root / "report_docx"
    assert sorted(p.name for p in (out_root / "report_docx" / "person_images").glob("*.png")) == [
        "report image (2).png", "report image.png"]

    # the OCR side is exactly what it always was
    ocr = client.get(f"/api/v1/documents/{doc_id}/results").json()
    assert ocr["results"]["extraction_status"] in ("complete", "partial") and ocr["results"]["fields"]

    path = wait_for(lambda: report_file(out_root, doc_id).exists() and report_file(out_root, doc_id),
                    what="report_result.json")
    combined = wait_for(lambda: (lambda d: d if d["processing"]["image_extraction_status"] == "succeeded" else None)(
        json.loads(path.read_text(encoding="utf-8"))), what="combined json with images")
    assert combined["schema_version"] == "1.0.0" and combined["document_id"] == doc_id
    assert combined["processing"]["ocr_status"] in ("succeeded", "partial")
    assert combined["processing"]["image_extraction_status"] == "succeeded"
    # the OCR payload is the existing /results response, untouched, and its own schema version is carried
    assert combined["ocr"]["payload"] == ocr
    assert combined["ocr"]["schema_version"] == ocr["results"]["schema_version"]
    assert combined["person_images"]["status"] == "succeeded" and len(combined["person_images"]["images"]) == 2
    first = combined["person_images"]["images"][0]
    assert first["file"] == "person_images/report image.png"  # relative to the document's folder
    assert set(first) >= {"file", "name", "source_page", "source_member", "width", "height", "faces", "size_bytes", "sha256"}
    assert (doc_folder(doc_id) / first["file"]).is_file()


def test_the_combined_json_has_no_absolute_paths_no_secrets_and_no_image_bytes(integrated, out_root, tmp_path):
    client = integrated
    doc_id = _upload_ok(client, _two_people_one_other(tmp_path))
    wait_images(client, doc_id)
    text = client.get(f"/api/v1/documents/{doc_id}/report_result").text
    manifest = (doc_folder(doc_id) / "person_images" / "manifest.json").read_text(encoding="utf-8")
    for blob in (text, manifest):
        assert str(tmp_path) not in blob and "C:\\" not in blob and "\\\\" not in blob.replace("\\\\n", "")
        assert ADMIN_KEY not in blob and "base64" not in blob.lower()
    assert "api_key" not in text.lower()


def test_documents_are_kept_apart_and_each_has_its_own_output_folder(integrated, out_root, tmp_path):
    client = integrated
    a = _upload_ok(client, make_docx(tmp_path / "a.docx", [make_png(RED, seed=1)]))
    b = _upload_ok(client, make_docx(tmp_path / "b.docx", [make_png(RED, seed=9), make_png(RED, seed=8)]))
    va, vb = wait_images(client, a), wait_images(client, b)
    assert a != b and len(va["images"]) == 1 and len(vb["images"]) == 2
    assert {p.name for p in out_root.iterdir() if p.is_dir()} == {"a_docx", "b_docx"}
    assert doc_folder(a).name == "a_docx" and doc_folder(b).name == "b_docx"
    assert [im["name"] for im in vb["images"]] == ["b image.png", "b image (2).png"]
    ra = store.summarize(store.read_manifest(a))["current"]["images"][0]["sha256"]
    rb = {im["sha256"] for im in store.summarize(store.read_manifest(b))["current"]["images"]}
    assert ra not in rb  # nothing leaks across documents
    # a document's image cannot be fetched through another document's id
    assert client.get(va["images"][0]["url"]).status_code == 200
    assert client.get(va["images"][0]["url"].replace(a, b)).status_code == 404


def test_two_uploads_with_the_same_file_name_get_their_own_folders(integrated, out_root, tmp_path):
    client = integrated
    first_dir = tmp_path / "one"
    second_dir = tmp_path / "two"
    first_dir.mkdir()
    second_dir.mkdir()
    a = _upload_ok(client, make_docx(first_dir / "Same Name.docx", [make_png(RED, seed=1)]))
    b = _upload_ok(client, make_docx(second_dir / "Same Name.docx", [make_png(RED, seed=2), make_png(RED, seed=3)]))
    va, vb = wait_images(client, a), wait_images(client, b)
    assert {p.name for p in out_root.iterdir() if p.is_dir()} == {"Same Name_docx", "Same Name_docx (2)"}
    assert len(va["images"]) == 1 and len(vb["images"]) == 2
    assert doc_folder(a).name == "Same Name_docx" and doc_folder(b).name == "Same Name_docx (2)"
    # each document's images are only in its own folder, and the index maps the ids to the right folders
    assert {im["name"] for im in vb["images"]} == {"Same Name image.png", "Same Name image (2).png"}
    assert len(list((out_root / "Same Name_docx" / "person_images").glob("*.png"))) == 1
    assert len(list((out_root / "Same Name_docx (2)" / "person_images").glob("*.png"))) == 2
    index = json.loads((out_root / store.INDEX_NAME).read_text(encoding="utf-8"))["documents"]
    assert index == {a: "Same Name_docx", b: "Same Name_docx (2)"}


def test_the_index_can_be_lost_and_folders_are_still_matched_to_their_documents(integrated, out_root, tmp_path):
    client = integrated
    a = _upload_ok(client, make_docx(tmp_path / "Keep Me.docx", [make_png(RED, seed=1)]))
    view = wait_images(client, a)
    (out_root / store.INDEX_NAME).unlink()
    assert doc_folder(a).name == "Keep Me_docx"  # recovered from the manifest inside the folder
    assert client.get(view["images"][0]["url"]).status_code == 200


def test_folders_from_the_earlier_layout_are_still_found_and_served(integrated, out_root, tmp_path):
    """Earlier versions named the folder by document id and kept images under runs/<run_id>/person_001.ext."""
    from tests.person_images.helpers import make_png as mk

    doc_id = "11111111-2222-3333-4444-555555555555"
    run_id = "run_20261015T044553Z_7dd58a"
    legacy = out_root / doc_id / "person_images" / "runs" / run_id
    legacy.mkdir(parents=True)
    (legacy / "person_001.jpeg").write_bytes(mk(RED))
    manifest = {"schema_version": "1.0.0", "document_id": doc_id, "runs": [{
        "run_id": run_id, "status": "succeeded", "images": [{"file": f"person_images/runs/{run_id}/person_001.jpeg",
                                                            "name": "person_001.jpeg", "width": 160, "height": 200,
                                                            "faces": 1, "size_bytes": 10, "sha256": "x"}]}]}
    (out_root / doc_id / "person_images" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert store.find_dir(doc_id) == out_root / doc_id
    assert store.resolve_image(doc_id, run_id, "person_001.jpeg") == legacy / "person_001.jpeg"


def test_pdf_images_carry_their_source_page(integrated, tmp_path):
    client = integrated
    doc_id = _upload_ok(client, make_pdf(tmp_path / "p.pdf", [make_png(RED, seed=4)]))
    view = wait_images(client, doc_id)
    assert view["status"] == "succeeded" and view["images"][0]["source_page"] == 1


# ---------------------------------------------------------------------------------------------------------
# 2. independent failures
# ---------------------------------------------------------------------------------------------------------

def test_ocr_succeeds_but_image_extraction_fails(integrated, out_root, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(f"model exploded at {tmp_path}\\secret.onnx")

    monkeypatch.setattr(extractor, "save_person_images", boom)
    client = integrated
    doc_id = _upload_ok(client, _two_people_one_other(tmp_path))
    view = wait_images(client, doc_id)
    assert view["status"] == "failed" and view["images"] == []
    assert str(tmp_path) not in view["error"] and "<path>" in view["error"]  # no local paths in error summaries
    ocr = client.get(f"/api/v1/documents/{doc_id}/results")
    assert ocr.status_code == 200 and ocr.json()["results"]["fields"]  # OCR is untouched
    combined = client.get(f"/api/v1/documents/{doc_id}/report_result").json()
    assert combined["processing"]["image_extraction_status"] == "failed"
    assert combined["processing"]["ocr_status"] in ("succeeded", "partial")
    assert combined["ocr"]["payload"] == ocr.json()  # the OCR data is all there
    assert combined["person_images"]["images"] == []


def test_images_succeed_but_ocr_fails(integrated, out_root, tmp_path):
    """A scanned (image-only) PDF with OCR disabled: OCR cannot read it, the image pipeline still finds the person."""
    client = integrated
    doc_id = _upload_ok(client, make_pdf(tmp_path / "scan.pdf", [make_png(RED, size=(400, 500), seed=5)], text=""))
    view = wait_images(client, doc_id)
    assert view["status"] == "succeeded" and len(view["images"]) == 1  # images are not discarded
    assert client.get(f"/api/v1/documents/{doc_id}").json()["document"]["status"] == "failed"
    combined = wait_for(lambda: (lambda d: d if d["processing"]["ocr_status"] == "failed" else None)(
        client.get(f"/api/v1/documents/{doc_id}/report_result").json()), what="failed ocr state")
    assert combined["processing"]["image_extraction_status"] == "succeeded"
    assert combined["ocr"]["payload"] is None and combined["ocr"]["schema_version"] is None  # nothing invented
    assert combined["processing"]["ocr_error_summary"]
    assert len(combined["person_images"]["images"]) == 1


def test_both_pipelines_fail_and_both_say_so(integrated, tmp_path):
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"%PDF-1.4\nthis is not a real pdf\n")  # passes the upload check, readable by neither pipeline
    client = integrated
    doc_id = _upload_ok(client, bad)
    view = wait_images(client, doc_id)
    assert view["status"] == "failed"
    combined = client.get(f"/api/v1/documents/{doc_id}/report_result").json()
    assert combined["processing"]["ocr_status"] == "failed" and combined["processing"]["image_extraction_status"] == "failed"
    assert combined["ocr"]["payload"] is None and combined["person_images"]["images"] == []


def test_no_person_images_is_a_normal_outcome_not_an_error(integrated, out_root, tmp_path):
    client = integrated
    doc_id = _upload_ok(client, make_docx(tmp_path / "n.docx", [make_png(BLUE, seed=1)]))
    view = wait_images(client, doc_id)
    assert view["status"] == "no_person_images" and view["images"] == [] and view["error"] is None
    folder = store.find_dir(doc_id)
    assert folder is not None and not [p for p in folder.rglob("*") if p.suffix.lower() in store.IMAGE_EXTS]


def test_a_file_type_only_the_image_pipeline_could_handle_is_not_accepted_at_all(integrated, tmp_path):
    """XLSX is supported by the image code but the OCR upload refuses it: no document, so nothing is started."""
    x = tmp_path / "book.xlsx"
    x.write_bytes(b"PK\x03\x04 fake xlsx")
    r = upload(integrated, x, content_type="application/octet-stream")
    assert r.status_code == 400  # the OCR application's own validation, unchanged


def test_a_stored_file_type_the_image_code_does_not_support_is_reported_as_unsupported(integrated, out_root, tmp_path):
    from app import db as dbmod
    from app.config import get_settings
    from app.repositories.document_repository import DocumentRepository

    settings = get_settings()
    stored = "abcd_note.txt"
    (settings.upload_dir / stored).write_bytes(b"hello")
    with dbmod.session_scope() as db:
        doc = DocumentRepository(db).create(original_filename="note.txt", stored_filename=stored,
                                            content_type="text/plain", size_bytes=5)
        doc_id = doc.id
    service.start(doc_id)
    view = wait_images(integrated, doc_id)
    assert view["status"] == "unsupported" and "supports" in view["error"]


# ---------------------------------------------------------------------------------------------------------
# 3. reprocessing, corrections, refresh
# ---------------------------------------------------------------------------------------------------------

def test_reprocessing_adds_a_run_and_keeps_the_previous_one(integrated, out_root, tmp_path):
    client = integrated
    doc_id = _upload_ok(client, _two_people_one_other(tmp_path))
    first = wait_images(client, doc_id)
    base = doc_folder(doc_id) / "person_images"
    first_files = {p.name: p.read_bytes() for p in base.glob("*.png")}
    assert sorted(first_files) == ["report image (2).png", "report image.png"]
    first_url = first["images"][0]["url"]
    r = client.post(f"/api/v1/documents/{doc_id}/reprocess")
    assert r.status_code == 200
    second = wait_for(lambda: (lambda v: v if v["latest_run_id"] != first["run_id"] and v["status"] == "succeeded" else None)(
        client.get(f"/api/v1/documents/{doc_id}/person-images").json()), timeout=30, what="second run")
    assert second["run_id"] != first["run_id"] and len(second["runs"]) == 2
    # the first run's files are exactly as they were; the re-run has its own folder with the same naming
    assert {p.name: p.read_bytes() for p in base.glob("*.png")} == first_files
    assert sorted(p.name for p in (base / "rerun_2").glob("*.png")) == ["report image (2).png", "report image.png"]
    assert second["images"][0]["file"] == "person_images/rerun_2/report image.png"
    assert client.get(first_url).status_code == 200  # the old run's image is still served
    assert client.get(second["images"][0]["url"]).status_code == 200


def test_a_failed_rerun_does_not_hide_earlier_images(integrated, out_root, tmp_path, monkeypatch):
    client = integrated
    doc_id = _upload_ok(client, _two_people_one_other(tmp_path))
    first = wait_images(client, doc_id)
    monkeypatch.setattr(extractor, "save_person_images", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
    assert client.post(f"/api/v1/documents/{doc_id}/person-images/run").status_code == 202
    view = wait_for(lambda: (lambda v: v if v["status"] == "failed" else None)(
        client.get(f"/api/v1/documents/{doc_id}/person-images").json()), what="failed rerun")
    assert view["run_id"] == first["run_id"] and len(view["images"]) == 2  # still showing the last good run
    combined = client.get(f"/api/v1/documents/{doc_id}/report_result").json()
    assert combined["processing"]["image_extraction_status"] == "failed"  # ... while the failure is not hidden
    assert len(combined["person_images"]["images"]) == 2


def test_corrections_are_in_the_database_and_flow_into_the_regenerated_export(integrated, out_root, tmp_path):
    client = integrated
    doc_id = _upload_ok(client, _two_people_one_other(tmp_path))
    wait_images(client, doc_id)
    results = client.get(f"/api/v1/documents/{doc_id}/results").json()["results"]
    fid = results["fields"][0]["id"]
    r = client.patch(f"/api/v1/documents/{doc_id}/fields", headers={"X-API-Key": OPERATOR_KEY},
                     json={"corrections": [{"field_id": fid, "corrected_value": "Corrected by a person",
                                            "review_status": "corrected"}]})
    assert r.status_code == 200  # the existing correction endpoint, behaving as before

    def corrected_in_file():
        data = json.loads(report_file(out_root, doc_id).read_text(encoding="utf-8"))
        field = next(f for f in data["ocr"]["payload"]["results"]["fields"] if f["id"] == fid)
        return data if field["corrected_value"] == "Corrected by a person" else None

    data = wait_for(corrected_in_file, what="the PATCH hook to refresh the export")  # refreshed by the hook
    assert fid in data["ocr"]["payload"]["field_corrections"]
    assert len(data["person_images"]["images"]) == 2
    assert client.get(f"/api/v1/documents/{doc_id}/report_result").json()["ocr"]["payload"] == \
        client.get(f"/api/v1/documents/{doc_id}/results").json()  # always equal to the database state


def test_the_export_can_be_regenerated_on_demand_from_the_database(integrated, out_root, tmp_path):
    client = integrated
    doc_id = _upload_ok(client, _two_people_one_other(tmp_path))
    wait_images(client, doc_id)
    path = report_file(out_root, doc_id)
    wait_for(path.exists, what="export")
    path.unlink()  # the file is derived data: deleting it loses nothing
    r = client.get(f"/api/v1/documents/{doc_id}/report_result")
    assert r.status_code == 200 and path.exists()
    assert client.get(f"/api/v1/documents/{doc_id}/report_result?download=true").headers["content-disposition"].startswith("attachment")


# ---------------------------------------------------------------------------------------------------------
# 4. files, security, UI
# ---------------------------------------------------------------------------------------------------------

def test_missing_image_files_are_reported_not_crashed_on(integrated, out_root, tmp_path):
    client = integrated
    doc_id = _upload_ok(client, _two_people_one_other(tmp_path))
    view = wait_images(client, doc_id)
    (doc_folder(doc_id) / view["images"][0]["file"]).unlink()
    again = client.get(f"/api/v1/documents/{doc_id}/person-images").json()
    assert [im["available"] for im in again["images"]] == [False, True] and again["images"][0]["url"] is None
    gone = quote(view["images"][0]["name"], safe="")
    assert client.get(f"/api/v1/documents/{doc_id}/person-images/files/{view['run_id']}/{gone}").status_code == 404
    assert client.get(f"/api/v1/documents/{doc_id}/report_result").status_code == 200  # the export still builds


def test_image_files_need_the_same_credentials_as_the_rest_of_the_api(integrated, tmp_path):
    client = integrated
    doc_id = _upload_ok(client, _two_people_one_other(tmp_path))
    view = wait_images(client, doc_id)
    url = view["images"][0]["url"]
    assert "report%20image.png" in url  # file names with spaces are URL-encoded
    assert client.get(url, headers={"X-API-Key": "wrong-key-wrong-key-123456"}).status_code == 401
    assert client.get(url, headers={"X-API-Key": VIEWER_KEY}).status_code == 200  # viewers may look
    assert client.post(f"/api/v1/documents/{doc_id}/person-images/run", headers={"X-API-Key": VIEWER_KEY}).status_code == 403


@pytest.mark.parametrize("run,name", [
    ("../../etc", "person_001.png"), ("run_20260101T000000Z_abcdef", "../manifest.json"),
    ("run_20260101T000000Z_abcdef", "..%2Fmanifest.json"), ("run_20260101T000000Z_abcdef", "person_001.png.exe"),
    ("run_20260101T000000Z_abcdef", "report.png"), ("RUN_1", "person_001.png"), ("run_20260101T000000Z_abcdef", "person_1.png"),
])
def test_path_traversal_and_odd_names_are_rejected(integrated, tmp_path, run, name):
    client = integrated
    doc_id = _upload_ok(client, _two_people_one_other(tmp_path))
    wait_images(client, doc_id)
    assert client.get(f"/api/v1/documents/{doc_id}/person-images/files/{run}/{name}").status_code == 404


def test_only_files_listed_in_the_manifest_are_served_even_with_the_real_run_id(integrated, tmp_path):
    client = integrated
    doc_id = _upload_ok(client, _two_people_one_other(tmp_path))
    view = wait_images(client, doc_id)
    run = view["run_id"]
    base = f"/api/v1/documents/{doc_id}/person-images/files/{run}/"
    for name in ("manifest.json", "..%2Fmanifest.json", "..%2F..%2Freport_result.json", "report%20image.png%00",
                 "report%20image%20%283%29.png", "%2e%2e%2f%2e%2e%2freport_result.json"):
        assert client.get(base + name).status_code == 404, name
    assert client.get(view["images"][0]["url"]).status_code == 200


def test_store_refuses_bad_identifiers_and_cannot_leave_the_output_folder(out_root):
    for bad in ("../x", "x", "", "..", "g" * 36, "00000000-0000-0000-0000-000000000000/../.."):
        with pytest.raises(ValueError):
            store.find_dir(bad)
    good = "11111111-2222-3333-4444-555555555555"
    assert store.find_dir(good) is None  # no folder until a run or an export creates one
    assert store.resolve_image(good, "../..", "x image.png") is None
    assert store.resolve_image(good, "run_20260101T000000Z_abcdef", "../../x.png") is None


@pytest.mark.parametrize("uploaded,folder,image", [
    ("Interrogation_report-RGIA PS-830-2025-Some Name.docx", "Interrogation_report-RGIA PS-830-2025-Some Name_docx",
     "Interrogation_report-RGIA PS-830-2025-Some Name image.png"),
    ("../../evil/..\\x.docx", "x_docx", "x image.png"),                      # separators and dots cannot escape
    ("a:b*c?d\"e<f>g|h.docx", "a_b_c_d_e_f_g_h_docx", "a_b_c_d_e_f_g_h image.png"),  # characters Windows forbids
    ("CON.docx", "_CON_docx", "_CON image.png"),                              # reserved device name
    ("  ..hidden..  .docx", "hidden_docx", "hidden image.png"),               # leading / trailing dots and spaces
    ("తెలుగు నివేదిక.docx", "తెలుగు నివేదిక_docx", "తెలుగు నివేదిక image.png"),  # non-ASCII names are kept
    (".docx", "document_docx", "document image.png"),                         # nothing usable left
])
def test_uploaded_file_names_become_safe_folder_and_image_names(out_root, uploaded, folder, image):
    assert store.folder_name_for(uploaded) == folder
    base = store.image_base_name(uploaded, out_root / folder)
    assert store.image_file_name(base, 1, ".png") == image
    for part in (folder, image):
        assert "/" not in part and "\\" not in part and ".." not in part.split(" ")[0][:2] and part == part.strip(" .")


def test_a_deep_output_location_shortens_the_folder_name_instead_of_failing(tmp_path, monkeypatch):
    deep = tmp_path
    for part in ("deeply", "nested", "output", "location", "for", "testing", "path", "limits", "on", "windows", "x" * 30):
        deep = deep / part
    monkeypatch.setenv("IR_PERSON_IMAGES_OUTPUT", str(deep))
    name = "Interrogation_report-Cyberabad-RGI Airport PS-830-2025-Some Person Name.docx"
    folder = store.ensure_dir("11111111-2222-3333-4444-555555555555", name)
    assert folder.is_dir() and len(folder.name) < len(store.folder_name_for(name))
    # a shallower location keeps more of the name, and a genuinely short one (like C:\...\IR_OCR\output) keeps all of it
    monkeypatch.setenv("IR_PERSON_IMAGES_OUTPUT", str(tmp_path / "out"))
    shallow = store.ensure_dir("22222222-2222-3333-4444-555555555555", name)
    assert len(shallow.name) > len(folder.name)
    assert store._folder_limit(Path("C:/Users/Dell/Desktop/IR_OCR/output")) == store.MAX_NAME  # the real default location
    assert store.folder_name_for(name, store._folder_limit(Path("C:/Users/Dell/Desktop/IR_OCR/output"))) == store.folder_name_for(name)


def test_very_long_names_are_shortened_so_paths_stay_bounded(out_root):
    long_name = ("A very long interrogation report file name " * 8) + ".docx"
    folder = store.folder_name_for(long_name)
    base = store.image_base_name(long_name, out_root / folder / "person_images" / "rerun_9")
    assert len(folder) <= store.MAX_NAME + len("_docx")
    full = out_root / folder / "person_images" / "rerun_9" / store.image_file_name(base, 99, ".jpeg")
    assert len(str(full)) <= store.MAX_PATH_CHARS + 20


def test_image_numbering_after_the_first_uses_brackets():
    assert [store.image_file_name("R", n, ".jpeg") for n in (1, 2, 3, 10)] == [
        "R image.jpeg", "R image (2).jpeg", "R image (3).jpeg", "R image (10).jpeg"]


def test_unknown_or_malformed_document_ids_are_404(integrated):
    for doc_id in ("00000000-0000-0000-0000-000000000000", "not-a-uuid"):
        for url in (f"/api/v1/documents/{doc_id}/person-images", f"/api/v1/documents/{doc_id}/report_result"):
            assert integrated.get(url).status_code == 404


def test_the_results_page_gets_the_new_tab_and_is_otherwise_unchanged(integrated, make_client, tmp_path, out_root):
    client = integrated
    doc_id = _upload_ok(client, _two_people_one_other(tmp_path))
    page = client.get(f"/results/{doc_id}")
    assert page.status_code == 200
    assert "/integrations/person-images/static/person_images.js" in page.text
    assert "/integrations/person-images/static/person_images.css" in page.text
    assert "/static/js/app.js" in page.text  # the existing script is still there
    stripped = page.text
    for needle in ('<link rel="stylesheet" href="/integrations/person-images/static/person_images.css?v=',
                   '<script src="/integrations/person-images/static/person_images.js?v='):
        start = stripped.index(needle)
        stripped = stripped[:start] + stripped[stripped.index(">", start) + 1:]
        if needle.startswith("<script"):
            stripped = stripped.replace("</script>", "", 1)
    assert 'data-document-id="%s"' % doc_id in stripped and "person_images" not in stripped
    # the new assets are served, and the CSP the application sets still allows them (same-origin script)
    js = client.get("/integrations/person-images/static/person_images.js")
    assert js.status_code == 200 and "Person Images" in js.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    # other pages are not touched
    assert "person_images" not in client.get("/").text and "person_images" not in client.get("/history").text


def test_the_upload_response_and_existing_endpoints_are_passed_through_unchanged(integrated, tmp_path, make_client):
    client = integrated
    r = upload(client, _two_people_one_other(tmp_path))
    assert r.status_code == 202 and set(r.json()) == {"document_id", "status", "message", "poll_url"}
    assert r.json()["poll_url"] == f"/api/v1/documents/{r.json()['document_id']}"
    assert client.get("/health").status_code in (200, 503) or client.get("/api/v1/health").status_code


def test_attach_is_idempotent_and_the_ocr_code_does_not_know_the_integration(make_client, out_root):
    from integrations.person_images.wiring import attach

    raw = make_client(enable_auth=True)
    app = raw.app
    attach(app)
    n_routes = len(app.routes)
    attach(app)
    assert len(app.routes) == n_routes
    root = Path(__file__).resolve().parents[2] / "app"
    offenders = [p.name for p in root.rglob("*.py") if "integrations" in p.read_text(encoding="utf-8", errors="ignore")]
    assert offenders == []  # nothing in the OCR application refers to the integration


def test_interrupted_runs_are_marked_failed_not_left_processing(integrated, out_root):
    doc_id = "11111111-2222-3333-4444-555555555555"
    run_id = store.new_run_id()
    store.add_run(doc_id, {"run_id": run_id, "status": store.PROCESSING, "images": []}, "x.docx")
    assert service.recover_interrupted() == 1
    run = store.read_manifest(doc_id)["runs"][0]
    assert run["status"] == "failed" and "restart" in run["error"]


def test_runs_started_in_the_same_second_get_different_ids():
    assert len({store.new_run_id() for _ in range(200)}) == 200


def test_report_result_schema_file_matches_the_envelope(integrated, tmp_path):
    schema_path = Path(__file__).resolve().parents[2] / "integrations" / "person_images" / "report_result.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    client = integrated
    doc_id = _upload_ok(client, _two_people_one_other(tmp_path))
    wait_images(client, doc_id)
    data = client.get(f"/api/v1/documents/{doc_id}/report_result").json()
    for key in schema["required"]:
        assert key in data
    assert schema["properties"]["schema_version"]["const"] == data["schema_version"]
    assert set(schema["properties"]["processing"]["required"]) <= set(data["processing"])
    assert set(schema["properties"]["person_images"]["required"]) <= set(data["person_images"])
