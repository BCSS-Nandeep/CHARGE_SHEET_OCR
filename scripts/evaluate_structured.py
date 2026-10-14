"""Measure structured-field accuracy of the VLM pipeline against verified ground truth.

    python scripts/evaluate_structured.py                      # synthetic documents, real model
    python scripts/evaluate_structured.py --private            # also the private (real) documents
    python scripts/evaluate_structured.py --docs synthetic_03_multipage --max-pages 3

This sends real requests to the configured model server (VLM_BASE_URL). That server may be shared
and handles one request at a time: the script refuses to start while it is busy (override with
--wait SECONDS to wait for it to become idle) and sends requests strictly one after another.
Digital pages cost one mapping request each (about a minute on a CPU server); scanned pages cost a
transcription (minutes) plus a mapping request.

The report holds counts only. Values from private documents are never written to it.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.services.extraction_service import ExtractionService  # noqa: E402
from app.services.pdf_service import PdfService  # noqa: E402
from app.services.source_prep import prepare_source  # noqa: E402
from app.services.vlm_client import VlmClient  # noqa: E402
from evaluation.ground_truth import load_ground_truth, locate_source  # noqa: E402
from evaluation.metrics import evaluate_document  # noqa: E402
from evaluation.structured_metrics import aggregate, evaluate_structured  # noqa: E402

ORDER = ["case_exact", "critical_exact", "case_missing", "case_wrong", "case_fabricated", "case_fabricated_flagged",
         "accused_names", "accused_names_loose", "accused_attrs", "accused_spurious", "witnesses_names",
         "witnesses_names_loose", "witnesses_attrs", "witnesses_spurious", "investigating_officers_names",
         "investigating_officers_names_loose", "investigating_officers_spurious",
         "legal_sections", "legal_sections_loose", "values_with_evidence"]


def wait_until_idle(client: VlmClient, seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        busy = client.server_busy()
        if busy is False:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--private", action="store_true", help="include private (real) ground truth")
    ap.add_argument("--docs", help="comma-separated document ids")
    ap.add_argument("--max-pages", type=int, default=0, help="skip documents with more pages (0 = no limit)")
    ap.add_argument("--wait", type=int, default=0, help="seconds to wait for an idle server (default: refuse if busy)")
    ap.add_argument("--out", default=str(ROOT / "docs" / "STRUCTURED_EVALUATION.md"))
    ap.add_argument("--dump", help="folder to save each *synthetic* document's structured JSON (never private ones)")
    args = ap.parse_args()

    settings = get_settings()
    client = VlmClient(settings)
    health = client.health()
    if not (health["reachable"] and health["model_available"]):
        print(f"Model server not usable: {health['error']}")
        return 2
    if not wait_until_idle(client, args.wait):
        print("The model server is busy with another request/application; not starting. Use --wait N to wait.")
        return 3

    wanted = set(args.docs.split(",")) if args.docs else None
    runs = []
    for gt in load_ground_truth(include_private=args.private):
        if wanted and gt.document_id not in wanted:
            continue
        src = locate_source(gt)
        if src is None:
            print(f"skip {gt.document_id}: source file not found")
            continue
        pages = PdfService.page_count(prepare_source(src))
        if args.max_pages and pages > args.max_pages:
            print(f"skip {gt.document_id}: {pages} pages > --max-pages")
            continue
        print(f"run  {gt.document_id} ({pages} page(s)) ...", flush=True)
        started = time.monotonic()
        data = ExtractionService().process(prepare_source(src))
        seconds = time.monotonic() - started
        sev = evaluate_structured(data, gt.expected, document_id=gt.document_id, private=gt.private)
        old = evaluate_document(data, gt.expected, document_id=gt.document_id, layout=gt.layout, private=gt.private)
        runs.append((gt, pages, seconds, data, sev, old))
        if args.dump and not gt.private and data.structured is not None:
            Path(args.dump).mkdir(parents=True, exist_ok=True)
            (Path(args.dump) / f"{gt.document_id}.json").write_text(data.structured.model_dump_json(indent=1), encoding="utf-8")
        print(f"     {seconds:.0f}s  status={data.extraction_status.value}  structured={data.structured.document_status.value if data.structured else '-'}")

    if not runs:
        print("nothing evaluated")
        return 4

    lines = [
        "# Structured extraction evaluation",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M}. Model `{settings.vlm_model}` (transcription: "
        f"`{settings.vlm_transcribe_engine}`, fallback: `{settings.vlm_fallback_engine}`). "
        "Counts are on a small development set: **not a general accuracy claim**.",
        "",
        "Exact match is strict (see `evaluation/structured_metrics.py`). Ground truth for real documents was "
        "transcribed by the assistant and still needs human confirmation.",
        "",
        "## Per document",
        "",
        "| document | pages | seconds | extraction | structured | case exact | critical exact | names (acc/wit/IO) | legal |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    private_n = 0
    for gt, pages, seconds, data, sev, old in runs:
        name = gt.document_id
        if gt.private:
            private_n += 1
            name = f"private-{private_n}"
        c = sev.c
        lines.append(
            f"| {name} | {pages} | {seconds:.0f} | {data.extraction_status.value} | "
            f"{data.structured.document_status.value if data.structured else '-'} | {c('case_exact').fmt()} | "
            f"{c('critical_exact').fmt()} | {c('accused_names').fmt()} / {c('witnesses_names').fmt()} / "
            f"{c('investigating_officers_names').fmt()} | {c('legal_sections').fmt()} |")
    total = aggregate([r[4] for r in runs])
    lines += ["", "## Totals", "", "| metric | result |", "|---|---|"]
    lines += [f"| {k} | {total[k].fmt()} |" for k in ORDER if k in total]
    old_total = {}
    for r in runs:
        for k, v in r[5].counts.items():
            old_total.setdefault(k, type(v)()).merge(v)
    lines += ["", "## Previous as-printed pipeline on the same runs (different measure)", "",
              "The as-printed fields have no fixed names, so the old metric only asks whether an expected value "
              "appears *somewhere* in a field. It is not comparable to exact-match-in-the-right-field above; "
              "it is shown only for context.", "", "| metric | result |", "|---|---|"]
    lines += [f"| {k} | {old_total[k].fmt()} |" for k in ("case_values", "names", "legal_sections", "completeness") if k in old_total]
    lines += ["", "## Mismatches", ""]
    for gt, _, _, _, sev, _ in runs:
        if gt.private:
            lines.append(f"- private document: {len(sev.mismatches)} mismatch(es) (values withheld)")
        else:
            lines.append(f"- **{gt.document_id}**: " + ("none" if not sev.mismatches else ""))
            lines += [f"  - {m}" for m in sev.mismatches]
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nreport written to {args.out}")
    for k in ORDER:
        if k in total:
            print(f"  {k:34s} {total[k].fmt()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
