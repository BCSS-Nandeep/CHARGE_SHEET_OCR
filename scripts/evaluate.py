"""Evaluate extraction against all available ground truth and write an accuracy report.

    python scripts/evaluate.py                 # documents with a text layer only
    python scripts/evaluate.py --ocr           # also scanned documents (local OCR, slow on CPU)
    python scripts/evaluate.py --no-private    # synthetic fixtures only

Ground truth:
  tests/fixtures/ground_truth/*.json   synthetic fixtures (committed)
  tests/fixtures/private/*.json        real documents (gitignored; source PDFs located by SHA-256
                                       under uploads/ or $EVAL_DOCS_DIR)

Outputs:
  docs/ACCURACY_REPORT.md                       metrics only — no personal data
  tests/fixtures/private/ACCURACY_DETAILS.md    per-field mismatches for real documents (gitignored)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

METRIC_ORDER = [
    ("classification", "Document classification accuracy"),
    ("case_values", "Expected case values found in a field"),
    ("case_values_form", "Expected case values found in a form field (not only the narrative)"),
    ("names", "Expected person names found in a form field"),
    ("legal_sections", "Expected act + section found together in one field"),
    ("completeness", "Completeness (expected items found in any field)"),
    ("labelled_fields", "Fields with a label (descriptive, not accuracy)"),
    ("ocr_lines", "OCR reference lines recognised (similarity >= 0.9)"),
]


def _git_rev() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() + (" + uncommitted changes" if dirty.stdout.strip() else "")
    except Exception:
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ocr", action="store_true", help="run local OCR for scanned documents")
    parser.add_argument("--no-private", action="store_true", help="skip real-document ground truth")
    parser.add_argument("--report", default=str(ROOT / "docs" / "ACCURACY_REPORT.md"))
    parser.add_argument("--details", default=str(ROOT / "tests" / "fixtures" / "private" / "ACCURACY_DETAILS.md"))
    parser.add_argument("--json", default=None, help="also write raw counts as JSON")
    args = parser.parse_args()

    os.environ["OCR_ENABLED"] = "true" if args.ocr else "false"
    from app.config import get_settings

    get_settings.cache_clear()
    from app.services.extraction_service import ExtractionService
    from evaluation.ground_truth import load_ground_truth, locate_source
    from evaluation.metrics import aggregate, evaluate_document

    gts = load_ground_truth(include_private=not args.no_private)
    svc = ExtractionService(db=None)
    evals, skipped = [], []
    outputs = {}  # document_id -> (data, page_texts) for the private details pass
    for gt in gts:
        pdf = locate_source(gt)
        if pdf is None:
            skipped.append((gt.document_id, "source PDF not found"))
            continue
        if gt.requires_ocr and not args.ocr:
            skipped.append((gt.document_id, "scanned — rerun with --ocr"))
            continue
        t0 = time.time()
        data = svc.process(pdf)
        page_texts = dict(svc.last_ocr_text) if gt.requires_ocr else None
        outputs[gt.document_id] = (data, page_texts)
        ev = evaluate_document(
            data,
            gt.expected,
            document_id=gt.document_id,
            layout=gt.layout,
            private=gt.private,
            page_texts=page_texts,
        )
        evals.append(ev)
        print(f"{gt.document_id:45s} {data.extraction_status.value:9s} {time.time() - t0:6.1f}s  "
              f"case={ev.c('case_values_form').fmt():14s} complete={ev.c('completeness').fmt():14s} "
              f"mismatches={len(ev.mismatches)}")

    total = aggregate(evals)
    synthetic = aggregate([e for e in evals if not e.private])
    real = aggregate([e for e in evals if e.private])

    lines = [
        "# Accuracy Report",
        "",
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} by `scripts/evaluate.py` at commit `{_git_rev()}`"
        f" — OCR {'enabled (local PaddleOCR)' if args.ocr else 'not run'}.",
        "",
        "**These are measured counts on a small dataset, not a general accuracy claim.** "
        "Extractor confidence scores are not accuracy and are not used here. Every value is shown as "
        "`ratio (numerator/denominator)`; see `evaluation/metrics.py` for exact definitions.",
        "",
        "Schema 2.0.0 returns each PDF's own labels and values (no fixed field list). The ground truth "
        "records expected *values*, not labels, so these metrics measure whether each expected value was "
        "found and paired into a form field; label text and expected-blank fields are not scored, and "
        "precision (extra / wrong fields) is not measured.",
        "",
        f"Dataset: {len(evals)} document(s) evaluated — {sum(1 for e in evals if e.private)} real "
        f"(ground truth transcribed from the source and awaiting human confirmation), "
        f"{sum(1 for e in evals if not e.private)} synthetic.",
        "",
        "**Development-set caveat:** the real documents were used while building and fixing the parser, and the "
        "newer synthetic samples were written by the same author after the parser. High scores here show the "
        "pipeline handles these documents; they do not estimate accuracy on unseen templates. Measure on a "
        "held-out set of manually verified charge sheets before operational use.",
    ]
    if skipped:
        lines += ["", "Not evaluated: " + "; ".join(f"`{d}` ({why})" for d, why in skipped) + "."]
    lines += ["", "## Aggregate", "", "| Metric | All | Real documents | Synthetic |", "|---|---|---|---|"]
    for key, label in METRIC_ORDER:
        if key in total:
            lines.append(
                f"| {label} | {total[key].fmt()} | {real[key].fmt() if key in real else '—'} | "
                f"{synthetic[key].fmt() if key in synthetic else '—'} |"
            )
    lines += ["", "## Per document", ""]
    keys = ["case_values", "case_values_form", "names", "legal_sections", "completeness"]
    lines.append("| Document | Layout | Extraction | " + " | ".join(keys) + " |")
    lines.append("|---|---|---|" + "---|" * len(keys))
    for ev in evals:
        cells = [ev.counts[k].fmt() if k in ev.counts else "—" for k in keys]
        lines.append(f"| `{ev.document_id}` | {ev.layout} | {ev.extraction_status} | " + " | ".join(cells) + " |")
    lines += ["", "## Mismatches (synthetic documents)", ""]
    for ev in evals:
        if ev.private:
            continue
        lines.append(f"- `{ev.document_id}`: " + ("none" if not ev.mismatches else ""))
        for m in ev.mismatches:
            lines.append(f"  - {m}")
        for n in ev.notes:
            lines.append(f"  - note: {n}")
    lines += [
        "",
        "Mismatch details for real documents are written to `tests/fixtures/private/ACCURACY_DETAILS.md` "
        "(gitignored) because they contain personal data. Shared reports only carry field names:",
        "",
    ]
    for ev in evals:
        if ev.private:
            lines.append(f"- `{ev.document_id}`: {len(ev.mismatches)} mismatch(es)")
            for m in ev.mismatches:
                lines.append(f"  - {m}")
            for n in ev.notes:
                lines.append(f"  - note: {n}")
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {args.report}")

    private_evals = [e for e in evals if e.private]
    if private_evals:
        # Same comparison with values shown, for the private (gitignored) details file only
        details = ["# Accuracy details — real documents (PRIVATE, do not share)", ""]
        for gt in gts:
            if not gt.private or gt.document_id not in outputs:
                continue
            data, page_texts = outputs[gt.document_id]
            ev = evaluate_document(data, gt.expected, document_id=gt.document_id, layout=gt.layout, private=False,
                                   page_texts=page_texts)
            details.append(f"## {gt.document_id}")
            details += [f"- {m}" for m in ev.mismatches] or ["- no mismatches"]
            details.append("")
        Path(args.details).parent.mkdir(parents=True, exist_ok=True)
        Path(args.details).write_text("\n".join(details), encoding="utf-8")
        print(f"Wrote {args.details}")

    if args.json:
        Path(args.json).write_text(
            json.dumps({k: {"num": v.num, "den": v.den} for k, v in total.items()}, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
