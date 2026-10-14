"""Build a CER/WER/field validation report from extraction vs a reference text file.

Usage:
  python scripts/validation_report.py --document path/to/file.docx --reference path/to/reference.txt
  python scripts/validation_report.py --hypothesis path/to/extracted.txt --reference path/to/reference.txt

Does not invent accuracy: without --reference, only extraction coverage notes are printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.text_metrics import build_validation_report


def _extract_text(path: Path) -> tuple[str, list, list[str]]:
    from app.services.extraction_service import ExtractionService

    data = ExtractionService().process(path)
    parts = []
    fields = []
    notes = []
    for f in data.fields:
        fields.append((f.label, f.value or ""))
        if f.label:
            parts.append(f"{f.label}: {f.value or ''}")
        else:
            parts.append(f.value or "")
    for s in data.page_summaries:
        notes.append(
            f"page {s.page_number}: status={s.page_status} chars={s.char_count} route={s.route}"
        )
        if s.char_count == 0:
            notes.append(f"page {s.page_number}: EMPTY — possible missing content")
    for w in data.extraction_warnings:
        notes.append(f"warning: {w}")
    return "\n".join(parts), fields, notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--document", type=Path, help="Source PDF/DOCX to extract")
    ap.add_argument("--hypothesis", type=Path, help="Already-extracted text file")
    ap.add_argument("--reference", type=Path, help="Verified reference text (required for CER/WER)")
    ap.add_argument("--expected-fields", type=Path, help="JSON list of [label, value] pairs")
    ap.add_argument("--out", type=Path, help="Write markdown report here")
    ap.add_argument("--json-out", type=Path, help="Write JSON report here")
    args = ap.parse_args()

    warnings = []
    page_notes: list[str] = []
    extracted_fields: list = []
    doc_id = "unknown"

    if args.document:
        doc_id = args.document.name
        hyp, extracted_fields, page_notes = _extract_text(args.document)
    elif args.hypothesis:
        doc_id = args.hypothesis.name
        hyp = args.hypothesis.read_text(encoding="utf-8")
    else:
        ap.error("Provide --document or --hypothesis")

    if not args.reference:
        print("No --reference supplied: CER/WER/field accuracy not computed.")
        print("Coverage notes:")
        for n in page_notes:
            print(f"  - {n}")
        warnings.append("Reference missing — rates not computed; not an accuracy claim.")
        report = build_validation_report(
            document_id=doc_id,
            reference_text="",
            hypothesis_text=hyp,
            extracted_fields=extracted_fields,
            pages_compared=max(1, sum(1 for n in page_notes if n.startswith("page "))),
            page_notes=page_notes,
            warnings=warnings,
        )
    else:
        ref = args.reference.read_text(encoding="utf-8")
        expected = []
        if args.expected_fields:
            expected = json.loads(args.expected_fields.read_text(encoding="utf-8"))
            expected = [(a, b) for a, b in expected]
        report = build_validation_report(
            document_id=doc_id,
            reference_text=ref,
            hypothesis_text=hyp,
            expected_fields=expected,
            extracted_fields=extracted_fields,
            pages_compared=max(1, sum(1 for n in page_notes if n.startswith("page "))),
            page_notes=page_notes,
            warnings=warnings,
        )

    md = report.to_markdown()
    print(md)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md, encoding="utf-8")
        print(f"Wrote {args.out}")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        print(f"Wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
