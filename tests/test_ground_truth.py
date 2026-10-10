"""Regression against verified ground truth (synthetic + real documents when available).

Each ground-truth file may list ``known_gaps``: mismatches that are accepted,
documented limitations. Any *other* mismatch fails the test, and a known gap
that no longer occurs must be removed from the file (so the list stays honest).
"""

from __future__ import annotations

import os

import pytest

from evaluation.ground_truth import load_ground_truth, locate_source
from evaluation.metrics import evaluate_document

GTS = load_ground_truth(include_private=True)


@pytest.mark.parametrize("gt", GTS, ids=[g.document_id for g in GTS])
def test_matches_ground_truth(gt, monkeypatch):
    pdf = locate_source(gt)
    if pdf is None:
        pytest.skip("source PDF not available on this machine")
    if gt.requires_ocr:
        if os.environ.get("RUN_OCR_TESTS") != "1":
            pytest.skip("scanned document: set RUN_OCR_TESTS=1 to run local OCR")
        monkeypatch.setenv("OCR_ENABLED", "true")
        from app.config import get_settings

        get_settings.cache_clear()
    from app.services.extraction_service import ExtractionService

    svc = ExtractionService(db=None)
    data = svc.process(pdf)
    ev = evaluate_document(
        data, gt.expected, document_id=gt.document_id, layout=gt.layout, private=gt.private,
        page_texts=svc.last_ocr_text if gt.requires_ocr else None,
    )
    known = set(gt.data.get("known_gaps", []))
    unexpected = [m for m in ev.mismatches if m not in known]
    fixed = sorted(known - set(ev.mismatches))
    assert not unexpected, "New mismatches:\n" + "\n".join(unexpected)
    assert not fixed, "Known gaps no longer occur — remove them from the ground truth file:\n" + "\n".join(fixed)
