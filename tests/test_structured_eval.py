"""The accuracy metrics themselves: known right / wrong / missing / fabricated answers give known counts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import get_settings
from evaluation.structured_metrics import evaluate_structured
from tests.fake_vlm import FakeVlm, rule_model

GT = Path(__file__).parent / "fixtures" / "ground_truth"


@pytest.fixture()
def run(monkeypatch, fixtures_dir):
    monkeypatch.setenv("VLM_ENABLED", "true")
    monkeypatch.setenv("VLM_BASE_URL", "http://127.0.0.1:8081/v1")
    monkeypatch.setenv("VLM_MODEL", "fake-model")
    get_settings.cache_clear()

    def go(answer, gt_name="synthetic_03_multipage", pdf="sample_03_multipage.pdf"):
        import app.services.extraction_service as es

        monkeypatch.setattr(es, "VlmClient", FakeVlm(answer=answer))
        data = es.ExtractionService().process(fixtures_dir / pdf)
        exp = json.loads((GT / f"{gt_name}.json").read_text(encoding="utf-8"))["expected"]
        return evaluate_structured(data, exp, document_id=gt_name, private=False)

    return go


def test_perfect_answers_score_perfectly(run):
    ev = run(rule_model)
    assert ev.c("case_exact").fmt() == "1.000 (5/5)"  # fir_date is blank in the ground truth
    assert ev.c("case_fabricated").num == 0 and ev.c("case_fabricated").den == 1
    assert ev.c("accused_names").fmt() == "1.000 (3/3)" and ev.c("witnesses_names").fmt() == "1.000 (1/1)"
    assert ev.c("legal_sections").fmt() == "1.000 (1/1)"
    assert ev.c("accused_spurious").num == 0
    assert ev.mismatches == []


def test_wrong_missing_fabricated_and_spurious_are_counted_separately(run):
    def answer(text):
        out = rule_model(text)
        case = out.setdefault("case", {})
        if "FIR No" in text:
            case["fir_number"] = "55/2023"  # wrong digit — but still on the page? no: grounded as unverified
        case.pop("district", None)  # missing
        case["fir_date"] = "12/05/2022"  # expected blank: fabricated (not on the page either)
        out["accused"] = out.get("accused", [])[:2]  # one accused lost
        if "absconding" in text:  # a person who is on the page, but not a witness in the ground truth
            out.setdefault("witnesses", []).append({"name": "Unknown person"})
        return out

    ev = run(answer)
    assert ev.c("case_wrong").num == 1 and ev.c("case_missing").num == 1
    assert ev.c("case_exact").num == 3 and ev.c("case_exact").den == 5
    assert ev.c("critical_exact").num == 1 and ev.c("critical_exact").den == 2  # charge sheet no. right, FIR no. wrong
    # the invented FIR date is not on the page: flagged unverified, not presented as extracted
    assert ev.c("case_fabricated").num == 0 and ev.c("case_fabricated_flagged").num == 1
    assert ev.c("accused_names").num == 2 and ev.c("accused_names").den == 3
    assert ev.c("witnesses_spurious").num == 1 and ev.c("witnesses_spurious").den == 2
    assert any("fir_number: wrong" in m for m in ev.mismatches)
    assert any("district: missing" in m for m in ev.mismatches)
    assert any("expected blank" in m and "flagged unverified" in m for m in ev.mismatches)


def test_conflicting_value_is_never_counted_correct():
    from app.schemas.charge_sheet import ChargeSheetData
    from app.schemas.structured import PageStructuredInfo
    from app.services.structured_mapper import merge_observations, observe_page

    pages = [observe_page({"case": {"fir_number": fir}}, f"FIR No | {fir}", page=n, text_engine="pdf_text")
             for n, fir in ((1, "55/2022"), (2, "56/2022"))]  # two values, both with evidence
    s = merge_observations(pages, page_infos=[PageStructuredInfo(page=1), PageStructuredInfo(page=2)])
    ev = evaluate_structured(ChargeSheetData(structured=s), {"case": {"fir_number": "55/2022"}},
                             document_id="x", private=False)
    assert ev.c("case_exact").num == 0 and ev.c("critical_exact").num == 0
    assert any("fir_number: conflicting" in m for m in ev.mismatches)


def test_an_unsupported_second_value_does_not_cost_accuracy_but_stays_visible(run):
    def answer(text):
        out = rule_model(text)
        if "Witnesses" in text:  # page 3 'reads' a district that is not on that page
            out.setdefault("case", {})["district"] = "South Zone"
        return out

    ev = run(answer)
    assert ev.c("case_exact").num == 5 and ev.mismatches == []


def test_private_runs_never_echo_values():
    from app.schemas.charge_sheet import ChargeSheetData
    from app.schemas.structured import blank_structured

    data = ChargeSheetData(structured=blank_structured())
    ev = evaluate_structured(data, {"case": {"fir_number": "SECRET-123"}, "accused": [{"name": "Hidden Name"}]},
                             document_id="x", private=True)
    assert ev.mismatches and not any("SECRET" in m or "Hidden" in m for m in ev.mismatches)
