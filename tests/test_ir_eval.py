"""The labelled-evaluation harness: metric arithmetic, ground-truth gating, saved outputs, missing inputs.

A fake local VLM replaces the model; the ground truth is small enough to score by hand.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import httpx
import pytest

from app.config import get_settings
from app.ir_standard.docx_markdown import docx_to_markdown
from app.ir_standard.docx_writer import render_docx
from app.ir_standard.validator import build_standard
from app.ir_standard.vlm_client import VlmClient
from evaluation import ir_standard_eval as ev

ROOT = Path(__file__).resolve().parents[1]

# The report: a DOCX drawn from this standard (ticked + bold selections, as the writer prints them)
SOURCE = {"s03_physical_features": {"sex": "Male", "built": "Stout", "height": "5.6-6.0 ft", "ear": "Normal",
                                    "language_dialect": "Telugu"}}
# What the "model" answers: built is wrong (Lean), language is wrong (Hindi)
MODEL = {3: {"sex": "Male", "built": "Lean", "height": "5.6-6.0 ft", "ear": "Normal", "language_dialect": "Hindi"}}
GT = {"report_id": "t", "status": "verified", "verified_by": "tester", "verified_at": "2026-10-14",
      "sections": {"s03_physical_features": {"sex": "Male", "built": "Stout", "height": "5.6-6.0 ft", "ear": "Normal",
                                             "language_dialect": "Telugu"}}, "ambiguous": {}}


@pytest.fixture()
def vlm(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("LLM_MODEL", "fake-vlm")
    get_settings.cache_clear()
    pages = ["page text"]

    def handler(request: httpx.Request) -> httpx.Response:
        content = json.loads(request.content)["messages"][-1]["content"]
        if isinstance(content, list):
            text = pages.pop(0) if pages else "x"
        else:
            m = re.search(r"SECTION: (\d+)\.", content)
            text = json.dumps(MODEL.get(int(m.group(1)), {}) if m else {})
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    return VlmClient(get_settings(), transport=httpx.MockTransport(handler)), get_settings()


@pytest.fixture()
def docx(tmp_path):
    p = tmp_path / "report.docx"
    render_docx(build_standard(SOURCE), p)
    return p


def _run(vlm, docx, out):
    client, settings = vlm
    return ev.run_report({"id": "t", "kind": "docx", "path": str(docx)}, out, client, settings)


def test_run_saves_every_output_without_touching_other_locations(vlm, docx, tmp_path):
    out = tmp_path / "run"
    meta = _run(vlm, docx, out)
    d = out / "t"
    assert meta["status"] == "completed" and meta["errors"] == [] and meta["total_seconds"] >= 0
    for name in ("raw_vlm_output.json", "evidence.json", "final_structured.json", "run_meta.json", "source_markdown.md"):
        assert (d / name).exists(), name
    raw = json.loads((d / "raw_vlm_output.json").read_text(encoding="utf-8"))
    s3 = raw["sections"]["s03_physical_features"]
    assert "SECTION: 3." in s3["prompt"] and "COLUMN" in s3["prompt"]  # the prompt is saved verbatim
    assert json.loads(s3["attempts"][0]["raw_response"])["built"] == "Lean"  # raw model answer kept
    evidence = json.loads((d / "evidence.json").read_text(encoding="utf-8"))
    marks = evidence["s03_physical_features"]["fields"]["built"]["marks"]
    assert {"label", "box", "bold", "yes", "state", "at"} <= set(marks[0])  # checkbox + formatting evidence kept
    assert any(m["label"] == "Stout" and m["box"] == "ticked" and m["bold"] for m in marks)


def test_metrics_match_a_hand_count_when_ground_truth_is_verified(vlm, docx, tmp_path):
    out = tmp_path / "run"
    _run(vlm, docx, out)
    res = ev.compare(out / "t", GT, "", "docx")
    assert res["status"] == "MEASURED"
    m = res["metrics"]
    # sex, height, ear right; language: the model said "Hindi" (not in the report) but the report prints exactly
    # "Telugu" for it, so that text was used and is right; only built is wrong
    assert (m["field_accuracy"]["num"], m["field_accuracy"]["den"]) == (4, 5)
    assert (m["field_accuracy_by_kind"]["single"]["num"], m["field_accuracy_by_kind"]["single"]["den"]) == (2, 3)
    assert (m["field_accuracy_by_kind"]["text"]["num"], m["field_accuracy_by_kind"]["text"]["den"]) == (2, 2)
    # sex TP, height TP; built: the unmarked "Lean" was left empty (no FP) and Stout is missed (FN)
    c = m["checkbox"]
    assert (c["tp"], c["fp"], c["fn"]) == (2, 0, 1) and c["precision"] == 1.0 and c["recall"] == round(2 / 3, 4)
    v = m["validation"]  # built is wrong and flagged; language is right but flagged (it was filled from the report)
    assert (v["tp"], v["fp"], v["fn"], v["tn"]) == (1, 1, 0, 3) and v["precision"] == 0.5 and v["recall"] == 1.0
    assert m["text_error_rates"]["cer"]["reference_chars"] == len("normal") + len("telugu")
    assert m["text_error_rates"]["cer"]["errors"] == 0
    assert "not measured" in m["bold_detection"]
    errs = {e["path"].split(".")[-1]: e for e in res["field_errors"]}
    assert set(errs) == {"built", "language_dialect"}
    assert errs["built"]["error_category"] == "field_mapping"  # Stout is ticked in the source; the model said Lean
    assert errs["built"]["validation_outcome"] == "tp" and errs["built"]["human_review_required"] is True
    assert errs["built"]["evidence"]["marks"] and errs["built"]["flags"][0]["code"] == "unsupported_selection"
    # "Hindi" is nowhere in the report, so it was dropped and the report's own "Telugu" was used (still flagged)
    assert errs["language_dialect"]["predicted"] == "Telugu" and errs["language_dialect"]["correct"] is True
    assert errs["language_dialect"]["validation_outcome"] == "fp"
    assert {x["code"] for x in errs["language_dialect"]["flags"]} == {"ungrounded_value_removed", "filled_from_report_text"}
    assert res["review_flags"]["fields_flagged"] >= 2 and res["review_flags"]["flag_rate"]["den"] > 0


def test_no_accuracy_metrics_without_verified_ground_truth(vlm, docx, tmp_path):
    out = tmp_path / "run"
    _run(vlm, docx, out)
    draft = {**GT, "status": ev.DRAFT}
    for gt, problem, why in ((None, "no ground-truth file", "no ground-truth file"), (draft, "", "draft_unverified")):
        res = ev.compare(out / "t", gt, problem, "docx")
        assert res["status"] == "NOT_MEASURED" and why in res["reason"]
        assert "metrics" not in res and "field_errors" not in res
        assert res["review_flags"]["fields_flagged"] >= 2 and res["time"]["total_seconds"] is not None  # these need no GT
        assert res["flagged_fields"]  # flagged fields are still listed with their evidence
    forced = ev.compare(out / "t", draft, "", "docx", allow_draft=True)
    assert forced["status"].startswith("UNVERIFIED_DRAFT") and "NOT_ACCURACY" in forced["status"]


def test_ambiguous_fields_are_excluded_from_accuracy_and_reported_separately(vlm, docx, tmp_path):
    out = tmp_path / "run"
    _run(vlm, docx, out)
    gt = json.loads(json.dumps(GT))
    gt["ambiguous"] = {"s03_physical_features.built": ["Stout", "Lean"]}
    del gt["sections"]["s03_physical_features"]["built"]
    res = ev.compare(out / "t", gt, "", "docx")
    m = res["metrics"]
    assert (m["field_accuracy"]["num"], m["field_accuracy"]["den"]) == (4, 4)  # built left out of the denominator
    # the model's unmarked "Lean" was left empty, and an empty answer is not an accepted reading
    assert m["ambiguous_fields"] == {"listed": 1, "model_matches_an_accepted_reading": 0}
    built = next(e for e in res["field_errors"] if e["path"].endswith("built"))
    assert built["error_category"] == "ambiguous_source_mark" and built["ambiguous"] is True


def test_unlabelled_fields_are_not_scored_and_the_report_says_how_many(vlm, docx, tmp_path):
    out = tmp_path / "run"
    _run(vlm, docx, out)
    gt = {**GT, "sections": {"s03_physical_features": {"sex": "Male"}}}
    m = ev.compare(out / "t", gt, "", "docx")["metrics"]
    assert (m["field_accuracy"]["num"], m["field_accuracy"]["den"]) == (1, 1)
    assert "template field(s) were not labelled" in m["denominator_note"]


def test_flagged_but_correct_value_is_a_validation_false_positive(vlm, docx, tmp_path):
    out = tmp_path / "run"
    _run(vlm, docx, out)
    # a correct value that still carries a review flag (here added to the saved output) counts against validation precision
    path = out / "t" / "final_structured.json"
    std = json.loads(path.read_text(encoding="utf-8"))
    std["review_flags"].append({"section": "s03_physical_features", "path": "s03_physical_features.sex",
                                "code": "weak_evidence", "detail": "test flag"})
    path.write_text(json.dumps(std), encoding="utf-8")
    gt = {**GT, "sections": {"s03_physical_features": {"ear": "Normal", "sex": "Male"}}}
    res = ev.compare(out / "t", gt, "", "docx")
    v = res["metrics"]["validation"]
    assert v["fp"] == 1 and v["tn"] == 1 and res["metrics"]["field_accuracy"]["num"] == 2
    row = res["field_errors"][0]
    assert row["correct"] is True and row["validation_outcome"] == "fp" and "flagged for review" in row["why"]


def test_markdown_report_states_denominators_and_lists_errors(vlm, docx, tmp_path):
    out = tmp_path / "run"
    _run(vlm, docx, out)
    res = ev.compare(out / "t", GT, "", "docx")
    ev.write_comparison(out / "t", res, GT)
    text = (out / "t" / "field_errors.md").read_text(encoding="utf-8")
    assert "field accuracy: 4/5" in text and "checkbox precision" in text and "| built |" in text
    for name in ("comparison.json", "field_errors.json", "ground_truth.json"):
        assert (out / "t" / name).exists()


def test_scanned_pdf_run_saves_the_page_transcription(vlm, tmp_path):
    client, settings = vlm
    pdf = ROOT / "tests" / "fixtures" / "sample_04_scanned_image.pdf"
    meta = ev.run_report({"id": "scan", "kind": "scan_clean", "path": str(pdf)}, tmp_path / "run", client, settings)
    raw = json.loads((tmp_path / "run" / "scan" / "raw_vlm_output.json").read_text(encoding="utf-8"))
    assert raw["pages"][0]["raw_response"] == "page text" and raw["pages"][0]["seconds"] >= 0
    assert meta["transcribe_seconds"] is not None and meta["render_seconds"] is not None


def test_a_failed_run_is_recorded_not_hidden(vlm, tmp_path):
    client, settings = vlm
    bad = tmp_path / "broken.docx"
    bad.write_bytes(b"not a zip")
    meta = ev.run_report({"id": "bad", "kind": "docx", "path": str(bad)}, tmp_path / "run", client, settings)
    assert meta["status"] == "failed" and meta["errors"]
    res = ev.compare(tmp_path / "run" / "bad", GT, "", "docx")
    assert res["status"] == "NOT_MEASURED" and "failed" in res["reason"]


def test_draft_ground_truth_comes_from_marks_and_leaves_weak_marks_to_the_labeller():
    ticked = docx_to_markdown_text(SOURCE)
    draft = ev.draft_ground_truth(ticked, "t")
    assert draft["status"] == ev.DRAFT and draft["verified_by"] is None
    phys = draft["sections"]["s03_physical_features"]
    assert phys["sex"] == "Male" and phys["built"] == "Stout" and phys["ear"] == "Normal"
    bold_only = "**3.PHYSICAL FEATURES**\n\n| **Sex**<br>**(1)** | **Built**<br>**(2)** |\n| --- | --- |\n| ☐ **Male**<br>☐ Female | ☐ Lean<br>☐ Stout |\n"
    d2 = ev.draft_ground_truth(bold_only, "t")
    assert "sex" not in d2["sections"].get("s03_physical_features", {})  # bold alone is not decided for the human
    assert d2["needs_decision"]["s03_physical_features.sex"] == {"ticked": [], "bold_or_yes_only": ["Male"]}
    assert d2["sections"]["s03_physical_features"]["built"] is None  # nothing marked at all


def docx_to_markdown_text(standard_raw) -> str:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.docx"
        render_docx(build_standard(standard_raw), p)
        return docx_to_markdown(p)


def _load_cli():
    spec = importlib.util.spec_from_file_location("run_ir_evaluation", ROOT / "scripts" / "run_ir_evaluation.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_missing_documents_are_reported_as_not_run_with_what_is_needed(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"reports": [
        {"id": "scan_clean_A", "kind": "scan_clean", "path": str(tmp_path / "missing.pdf"), "ground_truth": None},
        {"id": "scan_hard_A", "kind": "scan_difficult", "path": None, "ground_truth": None}]}), encoding="utf-8")
    out = tmp_path / "out"
    monkeypatch.setattr("sys.argv", ["x", "--manifest", str(manifest), "--compare-only", str(out)])
    assert _load_cli().main() == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert [r["status"] for r in summary] == ["NOT_RUN", "NOT_RUN"]
    assert "clean scanned interrogation report" in summary[0]["reason"] and "verified by a person" in summary[0]["reason"]
    assert "difficult scanned" in summary[1]["reason"]
    assert "NOT_RUN" in (out / "summary.md").read_text(encoding="utf-8")
