"""Evaluate the standard-template IR pipeline on labelled reports (current prompt, real local VLM).

    python scripts/run_ir_evaluation.py --manifest M.json [--out DIR] [--only ID] [--allow-draft]
    python scripts/run_ir_evaluation.py --manifest M.json --compare-only RUN_DIR      # re-score saved output
    python scripts/run_ir_evaluation.py --manifest M.json --make-gt-drafts           # pre-annotate (DOCX only)

Manifest (JSON)::

    {"reports": [{"id": "docx_A", "kind": "docx", "path": "C:/.../report.docx", "ground_truth": "C:/.../docx_A.gt.json"},
                 {"id": "scan_clean_A", "kind": "scan_clean", "path": "C:/.../scan.pdf", "ground_truth": "..."},
                 {"id": "scan_hard_A", "kind": "scan_difficult", "path": "C:/.../scan2.pdf", "ground_truth": "..."}]}

Output goes to a NEW timestamped folder (default ``~/IR_OCR_eval/runs/<time>``), never into the app's
data / uploads / database, and outside the repository: the reports contain personal data. Nothing is
overwritten. See ``evaluation/ir_standard_eval.py`` for the ground-truth format and the metric rules.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.ir_standard.docx_markdown import docx_to_markdown  # noqa: E402
from app.ir_standard.vlm_client import VlmClient, VlmError  # noqa: E402
from evaluation import ir_standard_eval as ev  # noqa: E402

NEEDED = {
    "docx": "a DOCX interrogation report",
    "scan_clean": "a clean scanned interrogation report (PDF of page images)",
    "scan_difficult": "a difficult scanned interrogation report (skewed / faint / photocopied PDF)",
}


def _missing_note(entry: dict) -> str:
    need = []
    if not entry.get("path") or not Path(entry["path"]).exists():
        need.append(f"source file: {NEEDED.get(entry['kind'], entry['kind'])}")
    gt = entry.get("ground_truth")
    if not gt or not Path(gt).exists():
        need.append("a ground-truth JSON verified by a person")
    return "; ".join(need)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", default=str(Path.home() / "IR_OCR_eval" / "runs" / datetime.now().strftime("%Y%m%d_%H%M%S")))
    ap.add_argument("--only", action="append", help="run only this report id (repeatable)")
    ap.add_argument("--allow-draft", action="store_true",
                    help="also score against a DRAFT ground truth; results are labelled agreement, not accuracy")
    ap.add_argument("--compare-only", metavar="RUN_DIR", help="re-score the output saved in RUN_DIR; no model calls")
    ap.add_argument("--make-gt-drafts", action="store_true", help="write draft ground truth from the documents' own marks")
    args = ap.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    entries = [e for e in manifest["reports"] if not args.only or e["id"] in args.only]
    out = Path(args.compare_only or args.out)

    if args.make_gt_drafts:
        drafts = out / "ground_truth_drafts"
        for e in entries:
            if e["kind"] != "docx" or not Path(e["path"]).exists():
                print(f"[{e['id']}] skipped: drafts are generated from DOCX marks only")
                continue
            target = drafts / f"{e['id']}.gt.json"
            if target.exists():
                print(f"[{e['id']}] {target} exists, not overwritten")
                continue
            draft = ev.draft_ground_truth(docx_to_markdown(Path(e["path"])), e["id"])
            ev._dump(target, draft)
            print(f"[{e['id']}] wrote {target}: {sum(len(v) for v in draft['sections'].values())} drafted field(s), "
                  f"{len(draft['needs_decision'])} need a decision")
        return 0

    settings = get_settings()
    client = None
    if not args.compare_only:
        try:
            client = VlmClient(settings)
            if not client.is_up():
                print(f"VLM server not reachable at {settings.llm_base_url}; start scripts/run_vlm_server.ps1")
                return 1
        except VlmError as exc:
            print(f"VLM unavailable: {exc}")
            return 1
    out.mkdir(parents=True, exist_ok=True)

    summary = []
    for e in entries:
        rid = e["id"]
        print(f"[{rid}] ({e['kind']})")
        gt, gt_problem = ev.load_ground_truth(Path(e["ground_truth"]) if e.get("ground_truth") else None)
        if not e.get("path") or not Path(e["path"]).exists():
            res = {"report_id": rid, "kind": e["kind"], "status": "NOT_RUN", "reason": "source file missing; need " + _missing_note(e)}
            ev._dump(out / rid / "comparison.json", res)
            summary.append(res)
            print("   NOT RUN:", res["reason"])
            continue
        if not args.compare_only:
            meta = ev.run_report(e, out, client, settings)
            print(f"   run {meta['status']} in {meta['total_seconds']} s; errors: {meta['errors'] or 'none'}")
        res = ev.compare(out / rid, gt, gt_problem, e["kind"], allow_draft=args.allow_draft)
        ev.write_comparison(out / rid, res, gt)
        summary.append(res)
        print(f"   {res['status']}" + (f": {res['reason']}" if res.get("reason") else ""))

    ev._dump(out / "summary.json", summary)
    lines = [f"# IR standard-template evaluation  ({datetime.now():%Y-%m-%d %H:%M})", "",
             "| report | kind | status | time (s) | fields flagged | field accuracy |", "|---|---|---|---|---|---|"]
    for r in summary:
        rf = r.get("review_flags", {})
        fa = (r.get("metrics") or {}).get("field_accuracy")
        lines.append(f"| {r['report_id']} | {r['kind']} | {r['status']} | {(r.get('time') or {}).get('total_seconds', '-')} | "
                     f"{rf.get('fields_flagged', '-')}/{rf.get('template_fields_in_found_sections', '-')} | "
                     f"{(str(fa['num']) + '/' + str(fa['den'])) if fa else 'not measured'} |")
    lines += ["", "Statuses: MEASURED = scored against verified ground truth; NOT_MEASURED = ground truth missing or "
              "unverified (flag rate and timing only); NOT_RUN = source file missing."]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nresults in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
