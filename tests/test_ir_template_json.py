"""Template JSON: the report as the standard template (section headings, fields / table columns, values)."""

from __future__ import annotations

import json
import re

import httpx
import pytest

from app.config import get_settings
from app.ir_standard.docx_writer import render_docx
from app.ir_standard.paths import flatten
from app.ir_standard.schema import FAMILY_RELATIONS, SECTIONS
from app.ir_standard.template_json import TEMPLATE_JSON_FORMAT, build_template_json
from app.ir_standard.validator import blank_standard, build_standard
from app.ir_standard.vlm_client import VlmClient
from tests.conftest import ADMIN_KEY, OPERATOR_KEY, REVIEWER_KEY
from tests.test_ir_review_docx import MODEL, SOURCE


def test_every_section_field_and_column_is_present_even_for_an_empty_report():
    out = build_template_json(blank_standard())
    assert out["format"] == TEMPLATE_JSON_FORMAT
    assert [s["number"] for s in out["sections"]] == [s.number for s in SECTIONS]  # narrative (0) then 1..32
    for sec, spec in zip(out["sections"], SECTIONS):
        assert sec["title"] == spec.title and sec["key"] == spec.key
        keys = [f["key"] for g in sec.get("groups", []) for f in g["fields"]]
        assert keys == [f.key for f in spec.fields]
        assert [t["key"] for t in sec.get("tables", [])] == [t.key for t in spec.tables]
        for t_json, t in zip(sec.get("tables", []), spec.tables):
            assert [c["key"] for c in t_json["columns"]] == [c.key for c in t.columns]
            assert t_json["rows"], "a table keeps at least one (blank) row"
            for row in t_json["rows"]:
                assert list(row["cells"]) == [c.key for c in t.columns]
    values = [f["value"] for s in out["sections"] for g in s.get("groups", []) for f in g["fields"]]
    assert all(v in ("", []) for v in values)  # blank, never missing and never null


def test_values_headings_and_groups_match_the_ui_field_list():
    std = build_standard({"s03_physical_features": {"sex": {"selected": "Male", "evidence": "tick"}, "ear": "Normal"},
                          "s09_regular_habits": {"habits": {"selected": ["Liquor", "Smoking"]}},
                          "s05_family_history": {"members": [{"relation": "Father", "name": "R Kumar"}]}})
    out = build_template_json(std)
    s3 = next(s for s in out["sections"] if s["number"] == 3)
    assert [g["heading"] for g in s3["groups"]] == ["Build and features (columns 1–12)",
                                                    "Identification marks and peculiarities (columns 13–20)"]
    sex = s3["groups"][0]["fields"][0]
    assert sex == {"key": "sex", "label": "Sex", "column": 1, "value": "Male", "review": "unreviewed"}
    assert next(f for f in s3["groups"][0]["fields"] if f["key"] == "ear")["value"] == "Normal"
    habits = next(s for s in out["sections"] if s["number"] == 9)["groups"][0]["fields"][0]
    assert habits["value"] == ["Liquor", "Smoking"]  # several-choice: a list
    family = next(s for s in out["sections"] if s["number"] == 5)["tables"][0]
    assert [r["cells"]["relation"] for r in family["rows"]] == list(FAMILY_RELATIONS)
    assert family["rows"][0]["cells"]["name"] == "R Kumar"
    # same values as the field list the screen shows
    flat = {f.id: f.value for f in flatten(std)}
    for s in out["sections"]:
        for g in s.get("groups", []):
            for f in g["fields"]:
                shown = flat[f"{s['key']}.{f['key']}"]
                assert (", ".join(f["value"]) if isinstance(f["value"], list) else f["value"]) == shown


def test_text_written_where_no_template_option_matches_is_kept_and_the_review_state_is_carried():
    std = build_standard({"s03_physical_features": {"height": "6.1 ft"}})
    std["sections"]["s03_physical_features"]["height"] = {"selected": None, "evidence": "written", "other": "6.1 ft"}
    review = {"s03_physical_features.height": {"status": "needs_review", "note": "not a template option"}}
    out = build_template_json(std, review, {"status": "draft_needs_review"})
    height = next(f for s in out["sections"] if s["number"] == 3 for g in s["groups"] for f in g["fields"] if f["key"] == "height")
    assert height["value"] == "6.1 ft" and height["review"] == "needs_review" and height["note"] == "not a template option"
    assert out["report"] == {"status": "draft_needs_review"}


def test_a_section_missing_from_the_report_is_marked_but_still_listed_blank():
    std = build_standard({}, not_found=["s28_source_of_procurement"])
    s28 = next(s for s in build_template_json(std)["sections"] if s["number"] == 28)
    assert s28["in_report"] is False and s28["tables"][0]["rows"][0]["cells"]["supplier"] == ""
    assert next(s for s in build_template_json(std)["sections"] if s["number"] == 3)["in_report"] is True


@pytest.fixture()
def api(make_client, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("LLM_MODEL", "fake")
    monkeypatch.setenv("IR_STANDARD_ENABLED", "true")
    get_settings.cache_clear()
    import app.services.extraction_service as es

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        content = json.loads(request.content)["messages"][-1]["content"]
        m = re.search(r"SECTION: (\d+)\.", content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(MODEL.get(int(m.group(1)), {}) if m else {})}}]})

    class Fake(es.IrStandardService):
        def client(self):
            if self._client is None:
                self._client = VlmClient(self.settings, transport=httpx.MockTransport(handler))
            return self._client

    monkeypatch.setattr(es, "IrStandardService", Fake)
    path = tmp_path / "report.docx"
    render_docx(build_standard(SOURCE), path)
    with make_client(enable_auth=True) as c:
        r = c.post("/api/v1/documents/upload", headers={"X-API-Key": ADMIN_KEY},
                   files={"file": (path.name, path.read_bytes(),
                                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        yield c, r.json().get("id") or r.json()["document_id"]


def test_endpoint_returns_the_template_json_with_corrections_and_review_state(api):
    c, doc_id = api
    h = {"X-API-Key": REVIEWER_KEY}
    body = c.get(f"/api/v1/documents/{doc_id}/template.json", headers=h).json()
    assert body["format"] == TEMPLATE_JSON_FORMAT and len(body["sections"]) == len(SECTIONS)
    assert body["report"]["status"] == "draft_needs_review" and body["report"]["fields_needing_review"] >= 1
    built = next(f for s in body["sections"] if s["number"] == 3 for g in s["groups"] for f in g["fields"] if f["key"] == "built")
    assert built["value"] == "" and built["review"] == "needs_review"  # the model's unmarked "Lean" was left empty
    c.patch(f"/api/v1/documents/{doc_id}/fields", headers={"X-API-Key": OPERATOR_KEY},
            json={"corrections": [{"field_id": "s03_physical_features.built", "corrected_value": "Stout",
                                   "review_status": "corrected"}]})
    body = c.get(f"/api/v1/documents/{doc_id}/template.json", headers=h).json()
    built = next(f for s in body["sections"] if s["number"] == 3 for g in s["groups"] for f in g["fields"] if f["key"] == "built")
    assert built["value"] == "Stout" and built["review"] == "corrected"
    dl = c.get(f"/api/v1/documents/{doc_id}/template.json?download=true", headers=h)
    assert dl.headers["content-disposition"].endswith('_template.json"') and json.loads(dl.content)["format"] == TEMPLATE_JSON_FORMAT
