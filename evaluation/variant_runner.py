"""Run the extraction pipeline on same-template variants and compare with their ground truth."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from evaluation.ground_truth import PRIVATE_GT_DIR, GroundTruth, locate_source
from evaluation.metrics import DocEvaluation, evaluate_document
from evaluation.variants import Variant, build_variant, load_spec

SPEC_DIR = PRIVATE_GT_DIR / "variant_specs"
OCR_CACHE_DIR = PRIVATE_GT_DIR / "ocr_cache"


@dataclass
class TemplateBench:
    template_id: str
    spec: dict
    ground_truth: GroundTruth
    pdf_path: Path
    base_pdf: object
    base_ocr: Optional[List[dict]]


@dataclass
class VariantOutcome:
    template_id: str
    seed: int
    description: str
    evaluation: DocEvaluation  # field names only (shareable)
    detailed: DocEvaluation  # with values (private)
    unexpected: List[str] = field(default_factory=list)


def load_benches() -> List[TemplateBench]:
    from app.services.pdf_service import PdfService

    benches = []
    if not SPEC_DIR.is_dir():
        return benches
    for spec_path in sorted(SPEC_DIR.glob("*.json")):
        spec = load_spec(spec_path)
        gt_path = PRIVATE_GT_DIR / spec["base_ground_truth"]
        data = json.loads(gt_path.read_text(encoding="utf-8"))
        gt = GroundTruth(data["document_id"], gt_path, data, private=True)
        pdf = locate_source(gt)
        if pdf is None:
            continue
        base_ocr = None
        if spec.get("ocr_cache"):
            cache = OCR_CACHE_DIR / spec["ocr_cache"]
            if not cache.exists():
                continue
            base_ocr = json.loads(cache.read_text(encoding="utf-8"))
        benches.append(TemplateBench(spec["template_id"], spec, gt, pdf, PdfService().extract(pdf), base_ocr))
    return benches


def _patched_service(variant: Variant, base_pdf):
    from app.services.extraction_service import ExtractionService
    from app.services.ocr_service import OcrLine, OcrResult

    svc = ExtractionService(db=None)
    pdf_result = variant.pdf_result if variant.pdf_result is not None else base_pdf
    svc.pdf.extract = lambda path: copy.deepcopy(pdf_result)
    if variant.ocr_pages is not None:
        pages = {p["page"]: p for p in variant.ocr_pages}
        svc.pdf.render_page_image = lambda path, page_number: str(page_number).encode()

        def recognize(png: bytes) -> OcrResult:
            p = pages[int(png.decode())]
            return OcrResult(
                engine="paddleocr (cached boxes)",
                lines=[OcrLine(l["text"], l["score"], tuple(l["box"])) for l in p["lines"]],
                image_width=p["width"],
                image_height=p["height"],
            )

        svc.ocr.recognize_png = recognize
        svc.ocr.is_available = lambda: True
    return svc


def run_variant(bench: TemplateBench, seed: int) -> VariantOutcome:
    variant = build_variant(bench.spec, bench.ground_truth.data, seed, base_pdf=bench.base_pdf,
                            base_ocr=bench.base_ocr)
    svc = _patched_service(variant, bench.base_pdf)
    data = svc.process(bench.pdf_path)
    expected = variant.ground_truth["expected"]
    page_texts = None  # OCR recognition is not exercised by variants (original boxes are reused)
    ev = evaluate_document(data, expected, document_id=bench.template_id, layout=bench.ground_truth.layout,
                           private=True, page_texts=page_texts)
    detailed = evaluate_document(data, expected, document_id=bench.template_id, layout=bench.ground_truth.layout,
                                 private=False, page_texts=page_texts)
    known = set(bench.ground_truth.data.get("known_gaps", []))
    unexpected = [m for m in ev.mismatches if m not in known]
    return VariantOutcome(bench.template_id, seed, variant.description, ev, detailed, unexpected)
