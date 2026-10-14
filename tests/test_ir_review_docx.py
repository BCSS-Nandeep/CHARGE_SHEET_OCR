"""DOCX review safety: flagged fields are visibly marked, values are never changed, drafts are not final.

Only a reviewer's explicit verification (the existing /verify step) turns a report into a final one;
extraction never does, and editing a verified report re-opens review.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import httpx
import pytest
from docx import Document

from app.config import get_settings
from app.ir_standard.docx_writer import NEEDS_REVIEW, ReviewState, render_docx
from app.ir_standard.evidence import EvidenceIndex
from app.ir_standard.schema import SECTION_BY_NUMBER
from app.ir_standard.validator import build_standard
from app.ir_standard.vlm_client import VlmClient
from tests.conftest import ADMIN_KEY, OPERATOR_KEY, REVIEWER_KEY
from tests.test_ir_evidence import S3_MD, WRONG

S3 = SECTION_BY_NUMBER[3]


def claims_final(text: str) -> bool:
    """True when the report presents itself as verified (the draft banner says NOT A VERIFIED FINAL REPORT)."""
    return "VERIFIED FINAL REPORT" in text.replace("NOT A VERIFIED FINAL REPORT", "")


def docx_text(content: bytes) -> str:
    """All text of a DOCX: body, tables and page headers."""
    doc = Document(io.BytesIO(content))
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                parts += [p.text for p in cell.paragraphs]
    for s in doc.sections:
        parts += [p.text for p in s.header.paragraphs]
    return "\n".join(parts)


@pytest.fixture()
def flagged_standard():
    """The Physical Features answers the real model got wrong, checked against the report's own marks."""
    index = EvidenceIndex(S3, S3_MD, "docx_native")
    return build_standard({S3.key: WRONG}, not_found=["s28_source_of_procurement"], indexes={S3.key: index})


# ---------------------------------------------------------------------------
# flagged fields are marked, values stay
# ---------------------------------------------------------------------------

def test_flagged_fields_are_marked_needs_review_with_the_reason(flagged_standard):
    text = docx_text(render_docx(flagged_standard))
    assert text.count(NEEDS_REVIEW) >= 6
    assert f"{NEEDS_REVIEW}: the option returned is not marked in the report (left empty)" in text  # height / hair / beard
    assert f"{NEEDS_REVIEW}: the report's own text was used instead of the model's value" in text  # deformities
    assert "the report marks several options differently" in text  # color
    assert f"{NEEDS_REVIEW}: section not found in the uploaded report" in text  # section 28


def test_predicted_values_are_never_changed_or_removed(flagged_standard):
    text = docx_text(render_docx(flagged_standard))
    # a value that has a report-side basis but is flagged stays exactly as returned
    assert "A Mole on the chest" in text  # deformities text taken from the neighbouring column: kept, flagged
    assert "☒ Stout" in text and "☒ Black" in text  # ticked in the report (with a stray "Yes" nearby)
    # an option the report does not mark is left empty, not printed as chosen, and the model's answer is kept in the JSON
    assert "☒ 5.1-5.5 ft" not in text and "☐ 5.1-5.5 ft" in text
    height = flagged_standard["sections"][S3.key]["height"]
    assert height["selected"] is None and height["verification"]["model_answer"] == ["5.1-5.5 ft"]


def test_unflagged_fields_get_no_marker(flagged_standard):
    flagged = {f["path"] for f in flagged_standard["review_flags"]}
    assert "s03_physical_features.sex" not in flagged and "s03_physical_features.eyes" not in flagged
    doc = Document(io.BytesIO(render_docx(flagged_standard)))
    sex_cell = next(c for t in doc.tables for r in t.rows for c in r.cells if "☒ Male" in c.text)
    assert NEEDS_REVIEW not in sex_cell.text


def test_review_summary_page_lists_every_open_item_with_its_value(flagged_standard):
    text = docx_text(render_docx(flagged_standard))
    n = len({f["path"] for f in flagged_standard["review_flags"]})
    assert f"REVIEW SUMMARY – {n} item(s) {NEEDS_REVIEW}" in text
    assert "Values are shown exactly as extracted; none was changed or removed." in text
    summary = text.split("REVIEW SUMMARY")[1]
    assert "Height" in summary and "(empty)" in summary and "Deformities/Peculiarities" in summary


# ---------------------------------------------------------------------------
# draft versus verified final
# ---------------------------------------------------------------------------

def test_a_rendered_report_is_a_draft_unless_a_reviewer_verified_it(flagged_standard):
    for review in (None, ReviewState()):
        text = docx_text(render_docx(flagged_standard, review=review))
        assert "DRAFT – NOT A VERIFIED FINAL REPORT" in text
        assert "Do not use as a final report until a reviewer verifies it" in text
        assert "DRAFT – NOT VERIFIED – NEEDS REVIEW" in text  # also in the page header
        assert not claims_final(text)


def test_a_draft_without_flags_still_says_it_is_not_verified():
    clean = build_standard({})
    clean["review_flags"] = []
    text = docx_text(render_docx(clean))
    assert "No automatic flags were raised, but no reviewer has verified this report yet." in text
    assert not claims_final(text)


def test_verified_report_is_marked_final_with_who_and_when(flagged_standard):
    state = ReviewState(final=True, verified_by="rita", verified_at="2026-10-14 10:30 UTC")
    text = docx_text(render_docx(flagged_standard, review=state))
    assert claims_final(text) and "Verified by rita on 2026-10-14 10:30 UTC." in text
    assert "VERIFIED FINAL REPORT – verified by rita" in text  # page header
    assert "DRAFT" not in text and NEEDS_REVIEW not in text and "REVIEW SUMMARY" not in text
    n = len({f["path"] for f in flagged_standard["review_flags"]})
    assert f"{n} field(s) were flagged for review during extraction" in text  # the history is not hidden
    assert "A Mole on the chest" in text  # values untouched in the final report too


def test_fields_a_reviewer_resolved_lose_their_marker_but_the_rest_keep_it(flagged_standard):
    base = docx_text(render_docx(flagged_standard))
    state = ReviewState(resolved={"s03_physical_features.height", "s03_physical_features.hair"})
    text = docx_text(render_docx(flagged_standard, review=state))
    assert text.count(NEEDS_REVIEW) < base.count(NEEDS_REVIEW)
    assert "the option returned is not marked in the report (left empty)" in text  # beard is still open
    n = len({f["path"] for f in flagged_standard["review_flags"]})
    assert f"REVIEW SUMMARY – {n - 2} item(s)" in text


def test_the_blank_template_carries_no_review_marking():
    text = docx_text(render_docx(None))
    assert NEEDS_REVIEW not in text and "DRAFT" not in text and "VERIFIED" not in text


# ---------------------------------------------------------------------------
# the workflow: draft -> correct -> verify -> final -> edit -> draft again
# ---------------------------------------------------------------------------

MODEL = {3: {"sex": "Male", "built": "Lean", "height": "5.6-6.0 ft", "ear": "Normal", "language_dialect": "Telugu"}}
SOURCE = {S3.key: {"sex": "Male", "built": "Stout", "height": "5.6-6.0 ft", "ear": "Normal", "language_dialect": "Telugu"}}


@pytest.fixture()
def workflow_client(make_client, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("LLM_MODEL", "fake")
    monkeypatch.setenv("IR_STANDARD_ENABLED", "true")
    get_settings.cache_clear()
    import app.services.extraction_service as es

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):  # the pipeline probes the server before using it
            return httpx.Response(200, json={"data": []})
        content = json.loads(request.content)["messages"][-1]["content"]
        m = re.search(r"SECTION: (\d+)\.", content)
        answer = MODEL.get(int(m.group(1)), {}) if m else {}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

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
        assert r.status_code in (200, 201, 202), r.text
        yield c, r.json().get("id") or r.json()["document_id"]


def _download(c, doc_id, key=REVIEWER_KEY):
    r = c.get(f"/api/v1/documents/{doc_id}/standard.docx", headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r, docx_text(r.content)


def test_workflow_draft_correct_verify_final_then_reopen(workflow_client):
    c, doc_id = workflow_client

    # 1. extraction never produces a final report: flagged value present, marked, banner says DRAFT
    r, text = _download(c, doc_id)
    assert r.headers["X-Report-Status"] == "draft-needs-review" and "DRAFT_needs_review" in r.headers["content-disposition"]
    assert "DRAFT – NOT A VERIFIED FINAL REPORT" in text
    assert "☒ Lean" not in text and "☐ Lean" in text  # the model's unmarked "Lean" is left empty, not shown as chosen
    assert f"{NEEDS_REVIEW}: the option returned is not marked in the report (left empty)" in text

    # 2. an operator cannot finalize (existing role rule) and the report stays a draft
    denied = c.post(f"/api/v1/documents/{doc_id}/verify", json={"verified": True, "notes": "x"},
                    headers={"X-API-Key": OPERATOR_KEY})
    assert denied.status_code == 403
    assert _download(c, doc_id)[0].headers["X-Report-Status"] == "draft-needs-review"

    # 3. a reviewer correction resolves that field: marker gone, corrected value shown, still a draft
    fix = c.patch(f"/api/v1/documents/{doc_id}/fields", headers={"X-API-Key": OPERATOR_KEY},
                  json={"corrections": [{"field_id": "s03_physical_features.built", "corrected_value": "Stout",
                                         "review_status": "corrected"}]})
    assert fix.status_code == 200, fix.text
    r, text = _download(c, doc_id)
    assert "☒ Stout" in text and "☒ Lean" not in text
    assert "the option returned is not marked in the report (left empty)" not in text
    assert r.headers["X-Report-Status"] == "draft-needs-review"

    # 4. verification needs notes (explicit human confirmation) and the reviewer role
    assert c.post(f"/api/v1/documents/{doc_id}/verify", json={"verified": True},
                  headers={"X-API-Key": REVIEWER_KEY}).status_code == 400
    ok = c.post(f"/api/v1/documents/{doc_id}/verify", json={"verified": True, "notes": "Checked against the report"},
                headers={"X-API-Key": REVIEWER_KEY})
    assert ok.status_code == 200, ok.text
    r, text = _download(c, doc_id)
    assert r.headers["X-Report-Status"] == "verified-final" and "DRAFT" not in r.headers["content-disposition"]
    assert claims_final(text) and "Verified by rita" in text and "DRAFT" not in text

    # 5. editing a verified report re-opens review: it is a draft again
    c.patch(f"/api/v1/documents/{doc_id}/fields", headers={"X-API-Key": OPERATOR_KEY},
            json={"corrections": [{"field_id": "s03_physical_features.ear", "corrected_value": "Large",
                                   "review_status": "corrected"}]})
    r, text = _download(c, doc_id)
    assert r.headers["X-Report-Status"] == "draft-needs-review" and not claims_final(text)


def test_the_json_keeps_flags_and_values_for_review_regardless_of_status(workflow_client):
    c, doc_id = workflow_client
    std = c.get(f"/api/v1/documents/{doc_id}/standard", headers={"X-API-Key": REVIEWER_KEY}).json()
    built = std["sections"][S3.key]["built"]
    assert built["selected"] is None and built["verification"]["model_answer"] == ["Lean"]  # left empty, answer kept
    assert any(f["code"] == "unsupported_selection" and f["path"].endswith("built") for f in std["review_flags"])
    assert std["sections"][S3.key]["built"]["verification"]["candidates"][0]["option"] == "Stout"
