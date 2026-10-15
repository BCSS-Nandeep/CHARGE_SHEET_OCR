"""The JSON tab's data: keys = the labels printed in the uploaded document, values = the complete final
text shown in the Fields tab. No model is involved (the VLM is off in tests)."""

from __future__ import annotations

import fitz
import pytest

from app.schemas.charge_sheet import PdfField
from app.services.fields_json import UNLABELLED, fields_to_json

TEMPLATE_A = [
    "CHARGE SHEET (U/S 173 Cr.P.C.)",
    "PS : Riverton",
    "District : Eastmoor",
    "FIR No. : 17/2021",
    "Charge sheet Dt : 05-02-2022",
    "Final result No./Year :",
    "Name of the IO (SI) : B. Karan, SI of Riverton P.S.",
    "Particulars of accused persons charge sheeted",
    "A1- Pavan Kalyan S/o Ravi, Age. 30 Yrs, Caste. Mala, Occ. Driver, R/o Main Road, Riverton.",
    "(Arrested the accused Person A-1 on 01-02-2021 and produced before the Hon'ble Court for Judicial Remand.)",
    "A2- Salma Begum W/o Imran, Age. 28 Yrs, Caste. Muslim, Occ. Tailor, R/o Old Town, Riverton.",
    "(Arrested the accused Person A-2 on 03-02-2021 and released on bail.)",
    "Particulars of the accused absconding",
    "NIL",
    "List of witnesses",
    "LW-1 Meena Devi W/o Kumar, Age. 40 Yrs, R/o Lake Road. Eye witness.",
    "LW-2 Dr. Hari, Medical Officer, District Hospital. Medical evidence.",
]

# a different template: other labels, other order, other wording
TEMPLATE_B = [
    "FINAL REPORT",
    "Police Station : Lakeside",
    "Dist. : Westvale",
    "Crime No. : 8/2022",
    "Date of submission : 12-04-2022",
    "Acts and sections : u/s 379 IPC",
    "List of accused persons",
    "1. Anil Kumar S/o Raju, Age 33, R/o Hill Street.",
    "(Arrested on 02-03-2022 and remanded to judicial custody for 14 days.)",
    "Property seized : Two-wheeler, mobile phone",
]


def make_pdf(path, lines):
    doc = fitz.open()
    page = doc.new_page()
    y = 60
    for ln in lines:
        page.insert_text((50, y), ln, fontsize=10)
        y += 18
    doc.save(str(path))
    return path


def upload(client, path):
    res = client.post("/api/v1/documents/upload", files={"file": (path.name, path.read_bytes(), "application/pdf")})
    assert res.status_code == 202, res.text
    return res.json()["document_id"]


def fields_of(client, doc_id):
    return client.get(f"/api/v1/documents/{doc_id}/results").json()["results"]["fields"]


def shown(f):  # the Fields tab's rule: a saved correction, else the extracted value
    cv = f.get("corrected_value")
    return cv if cv not in (None, "") else (f["value"] or "")


# ---------------------------------------------------------------- the key / value rule


def pf(label, value, corrected=None, i=1):
    return PdfField(id=f"p1-{i:02d}", page=1, label=label, value=value, corrected_value=corrected)


def test_keys_are_the_labels_as_printed_and_values_are_complete_and_untouched():
    long_value = "line one\nline two\n\nline four with   odd   spacing and a trailing dot."
    out = fields_to_json([pf("Particulars of accused persons charge sheeted", long_value), pf("PS", "Riverton", i=2)])
    assert list(out) == ["Particulars of accused persons charge sheeted", "PS"]
    assert out["Particulars of accused persons charge sheeted"] == long_value  # not trimmed, split or rewritten


def test_missing_values_stay_empty_and_nothing_is_invented():
    out = fields_to_json([pf("Final result No./Year", ""), pf("District", "Eastmoor", i=2)])
    assert out == {"Final result No./Year": "", "District": "Eastmoor"}
    assert fields_to_json([]) == {}


def test_a_saved_correction_is_the_final_value_and_an_empty_correction_is_not_one():
    out = fields_to_json([pf("FIR No.", "17/2021", corrected="18/2021"), pf("Year", "2021", corrected="", i=2)])
    assert out == {"FIR No.": "18/2021", "Year": "2021"}


def test_text_before_any_label_and_repeated_labels_keep_every_occurrence():
    out = fields_to_json([pf(None, "CHARGE SHEET"), pf("Witness", "A", i=2), pf("Witness", "B", i=3), pf("Witness", "C", i=4)])
    assert out == {UNLABELLED: "CHARGE SHEET", "Witness": "A", "Witness (2)": "B", "Witness (3)": "C"}


# ---------------------------------------------------------------- through the API, two different templates


@pytest.mark.parametrize("lines", [TEMPLATE_A, TEMPLATE_B], ids=["template-a", "template-b"])
def test_json_equals_the_fields_tab_for_each_template(client, tmp_path, lines):
    doc_id = upload(client, make_pdf(tmp_path / "t.pdf", lines))
    fields = fields_of(client, doc_id)
    body = client.get(f"/api/v1/documents/{doc_id}/json").json()
    expected_keys = [f["label"] or UNLABELLED for f in fields]
    assert [k.split(" (")[0] if k.endswith(")") and k not in expected_keys else k for k in body] == expected_keys
    assert list(body.values()) == [shown(f) for f in fields]  # same values, same order


def test_template_a_keeps_each_complete_printed_field_as_one_key(client, tmp_path):
    body = client.get(f"/api/v1/documents/{upload(client, make_pdf(tmp_path / 'a.pdf', TEMPLATE_A))}/json").json()
    accused = body["Particulars of accused persons charge sheeted"]
    for part in ("A1- Pavan Kalyan S/o Ravi", "Judicial Remand.)", "A2- Salma Begum W/o Imran", "released on bail.)"):
        assert part in accused  # both accused with their arrest / remand notes, in one value
    assert accused.index("A1-") < accused.index("Judicial Remand") < accused.index("A2-") < accused.index("released on bail")
    assert not any(k.startswith("(Arrested") for k in body)  # an arrest note is never a key
    witnesses = body["List of witnesses"]
    assert "LW-1 Meena Devi" in witnesses and "LW-2 Dr. Hari" in witnesses and "\n" in witnesses
    assert body["Particulars of the accused absconding"] == "NIL"
    assert body["PS"] == "Riverton" and body["FIR No."] == "17/2021" and body["Name of the IO (SI)"].startswith("B. Karan")


def test_missing_value_stays_empty_in_the_json(client, tmp_path):
    body = client.get(f"/api/v1/documents/{upload(client, make_pdf(tmp_path / 'a.pdf', TEMPLATE_A))}/json").json()
    assert "Final result No./Year" in body and body["Final result No./Year"] == ""


def test_template_b_adapts_to_its_own_labels(client, tmp_path):
    body = client.get(f"/api/v1/documents/{upload(client, make_pdf(tmp_path / 'b.pdf', TEMPLATE_B))}/json").json()
    assert body["Police Station"] == "Lakeside" and body["Crime No."] == "8/2022"
    assert "PS" not in body and "FIR No." not in body and "Year" not in body  # keys the document does not have
    accused = body["List of accused persons"]
    assert "Anil Kumar" in accused and "remanded to judicial custody" in accused
    assert body["Property seized"] == "Two-wheeler, mobile phone"


def test_every_value_in_the_json_comes_from_the_document(client, tmp_path):
    body = client.get(f"/api/v1/documents/{upload(client, make_pdf(tmp_path / 'a.pdf', TEMPLATE_A))}/json").json()
    printed = " ".join(" ".join(TEMPLATE_A).split())
    for key, value in body.items():
        for line in value.split("\n"):
            assert " ".join(line.split()) in printed, (key, line)  # nothing added or rewritten


# ---------------------------------------------------------------- corrections


def test_a_saved_correction_appears_in_the_json_without_reprocessing(client, tmp_path):
    doc_id = upload(client, make_pdf(tmp_path / "a.pdf", TEMPLATE_A))
    fields = fields_of(client, doc_id)
    by_label = {f["label"]: f for f in fields}
    fir, accused = by_label["FIR No."], by_label["Particulars of accused persons charge sheeted"]
    new_accused = accused["value"] + "\nA3- Added By Reviewer, Age. 22 Yrs."
    res = client.patch(f"/api/v1/documents/{doc_id}/fields", json={"corrections": [
        {"field_id": fir["id"], "corrected_value": "18/2021"},
        {"field_id": accused["id"], "corrected_value": new_accused}]})
    assert res.status_code == 200
    body = client.get(f"/api/v1/documents/{doc_id}/json").json()
    assert body["FIR No."] == "18/2021" and body["Particulars of accused persons charge sheeted"] == new_accused
    assert body["PS"] == "Riverton"  # untouched fields unchanged
    # the extracted value is still kept apart from the correction (history and results keep both)
    assert {f["label"]: f["value"] for f in fields_of(client, doc_id)}["FIR No."] == "17/2021"
    hist = client.get(f"/api/v1/documents/{doc_id}/corrections").json()
    assert {h["original_value"] for h in hist if h["canonical_name"] == fir["id"]} == {"17/2021"}


def test_editing_verification_and_reprocess_still_work(client, tmp_path):
    doc_id = upload(client, make_pdf(tmp_path / "a.pdf", TEMPLATE_A))
    fir = next(f for f in fields_of(client, doc_id) if f["label"] == "FIR No.")
    client.patch(f"/api/v1/documents/{doc_id}/fields", json={"corrections": [{"field_id": fir["id"], "corrected_value": "99/2021"}]})
    assert client.post(f"/api/v1/documents/{doc_id}/verify", json={"verified": True},
                       headers={"X-API-Key": "test-reviewer-key-0123456789"}).status_code == 400  # notes required
    ok = client.post(f"/api/v1/documents/{doc_id}/verify", json={"verified": True, "notes": "checked"},
                     headers={"X-API-Key": "test-reviewer-key-0123456789"})
    assert ok.status_code == 200 and ok.json()["document"]["verified"] is True
    assert client.get(f"/api/v1/documents/{doc_id}/json").json()["FIR No."] == "99/2021"
    client.post(f"/api/v1/documents/{doc_id}/reprocess")
    body = client.get(f"/api/v1/documents/{doc_id}/json").json()
    assert body["FIR No."] == "99/2021"  # the machine value is unchanged, so the correction is re-applied
    assert client.get(f"/api/v1/documents/{doc_id}").json()["document"]["verified"] is False  # new extraction: not verified


# ---------------------------------------------------------------- endpoint behaviour


def test_download_is_indented_valid_json_with_non_ascii_kept(client, tmp_path):
    import json

    doc_id = upload(client, make_pdf(tmp_path / "a.pdf", TEMPLATE_A))
    res = client.get(f"/api/v1/documents/{doc_id}/json?download=true")
    assert "attachment" in res.headers["content-disposition"] and res.headers["content-disposition"].endswith('.json"')
    assert res.text.startswith("{\n  \"") and json.loads(res.text) == client.get(f"/api/v1/documents/{doc_id}/json").json()


def test_json_endpoint_states(client, fixtures_dir):
    assert client.get("/api/v1/documents/00000000-0000-0000-0000-000000000000/json").status_code == 404
    pdf = fixtures_dir / "sample_04_scanned_image.pdf"  # scanned, OCR and VLM are off: extraction fails
    doc_id = upload(client, pdf)
    assert client.get(f"/api/v1/documents/{doc_id}/json").status_code == 404
    assert client.get(f"/api/v1/documents/{doc_id}/json", headers={"X-API-Key": ""}).status_code == 401


# ---------------------------------------------------------------- the page: tabs


def test_page_scripts_have_no_structured_tab_and_a_json_tab(client):
    js = client.get("/static/js/app.js").text
    assert 'data-tab="structured"' not in js and "Structured" not in js and "raw_json" not in js
    assert '["json", "JSON"]' in js and "/json" in js
    assert client.get("/static/js/structured.js").status_code == 404
    html = client.get("/").text
    assert "structured.js" not in html


# ---------------------------------------------------------------- the builder rule behind "one key per printed field"


def test_a_bracketed_note_is_never_a_label_and_stays_with_the_value_above():
    from app.services.field_builder import build_fields
    from app.services.form_parser import DocItem

    def run(lines):
        items = [DocItem(1, float(i), t) for i, t in enumerate(lines)]
        return {f.label: f.value for f in build_fields([items])}

    out = run(["Particulars of the accused", "A1- Pavan Kalyan S/o Ravi, Age. 30 Yrs.",
               "(Arrested on 01-02-2021 and produced before the Hon'ble Court", "for Judicial Remand.)",
               "A2- Salma Begum W/o Imran, Age. 28 Yrs.", "District : Eastmoor"])
    accused = out["Particulars of the accused"]
    assert accused.splitlines() == ["A1- Pavan Kalyan S/o Ravi, Age. 30 Yrs.",
                                    "(Arrested on 01-02-2021 and produced before the Hon'ble Court", "for Judicial Remand.)",
                                    "A2- Salma Begum W/o Imran, Age. 28 Yrs."]
    assert out["District"] == "Eastmoor"  # the next real label still starts a new field
    assert not any((k or "").startswith("(") for k in out)
    # a bracket at the very start (nothing above to attach to) does not crash or vanish
    first = run(["(Copy for office use)", "District : Eastmoor"])
    assert first["District"] == "Eastmoor" and any("Copy for office use" in (v or "") or "Copy for office use" in (k or "") for k, v in first.items())
