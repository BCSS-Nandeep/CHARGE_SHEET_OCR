"""Evaluate extraction against manually verified FIR 83/2020 ground truth.

Reports measured accuracy separately from model confidence scores.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.extraction_service import ExtractionService

GT_PATH = ROOT / "tests" / "fixtures" / "ground_truth_fir83_2020.json"
REPORT_PATH = ROOT / "docs" / "ACCURACY_REPORT_FIR83.md"


def norm(v: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (v or "").lower())


def contains(hay: str, needle: str) -> bool:
    return norm(needle) in norm(hay)


def main() -> int:
    gt = json.loads(GT_PATH.read_text(encoding="utf-8"))
    pdf = ROOT / gt["source_file"]
    if not pdf.exists():
        print("PDF missing:", pdf)
        return 1

    data = ExtractionService(db=None).process(pdf)

    # --- Scalar field exact-match / mapping ---
    scalar_correct = scalar_incorrect = scalar_missing = 0
    scalar_rows = []
    for key, expected in gt["scalar_fields"].items():
        field = getattr(data.case, key, None)
        got = None
        if field:
            got = field.normalized_value or field.original_value
        if got is None or str(got).strip() == "":
            scalar_missing += 1
            scalar_rows.append((key, expected, None, "missing"))
        elif contains(str(got), expected) or contains(expected, str(got)):
            scalar_correct += 1
            scalar_rows.append((key, expected, got, "correct"))
        else:
            scalar_incorrect += 1
            scalar_rows.append((key, expected, got, "incorrect"))

    scalar_denom = len(gt["scalar_fields"])
    scalar_acc = scalar_correct / scalar_denom if scalar_denom else 0

    # --- Legal sections entity accuracy ---
    got_sections = []
    for s in data.acts_and_sections:
        act = (s.act.normalized_value or s.act.original_value) if s.act else ""
        sec = (s.section.normalized_value or s.section.original_value) if s.section else ""
        got_sections.append((str(act or ""), str(sec or "")))

    sec_correct = 0
    for exp in gt["legal_sections"]:
        matched = False
        for act, sec in got_sections:
            if contains(act, exp["act"]) and contains(sec, exp["section"].split()[0]):
                matched = True
                break
        if matched:
            sec_correct += 1
    sec_denom = len(gt["legal_sections"])
    sec_acc = sec_correct / sec_denom if sec_denom else 0

    # --- Accused entity accuracy ---
    acc_correct = acc_incorrect = acc_missing = 0
    for exp in gt["accused"]:
        if not data.accused_persons:
            acc_missing += 1
            continue
        a = data.accused_persons[0]
        name_ok = a.name and contains(str(a.name.original_value), exp["name"].split()[0])
        age_ok = a.age and str(a.age.original_value) == exp["age"]
        parent_ok = a.parent_or_spouse and contains(
            str(a.parent_or_spouse.original_value), exp["parent_or_spouse_contains"]
        )
        addr_ok = a.address and contains(str(a.address.original_value), exp["address_contains"])
        checks = [name_ok, age_ok, parent_ok, addr_ok]
        if all(checks):
            acc_correct += 1
        elif any(checks):
            acc_incorrect += 1
        else:
            acc_missing += 1
    acc_denom = len(gt["accused"])
    acc_acc = acc_correct / acc_denom if acc_denom else 0

    # --- Witnesses ---
    wit_correct = 0
    for exp in gt["witnesses"]:
        found = False
        for w in data.witnesses:
            name = (w.name.original_value if w.name else "") or ""
            if contains(name, exp["name_contains"]):
                found = True
                break
        if found:
            wit_correct += 1
    wit_denom = len(gt["witnesses"])
    wit_acc = wit_correct / wit_denom if wit_denom else 0

    # --- Conflict detection rate ---
    conflict_hits = 0
    for exp in gt["expected_conflicts"]:
        ok = False
        for c in data.source_conflicts or []:
            if exp["field_contains"] not in c.field_name:
                continue
            vals = " ".join(c.values)
            if "values_include" in exp and all(v in c.values for v in exp["values_include"]):
                ok = True
            if "values_include_any" in exp and any(v.lower() in vals.lower() for v in exp["values_include_any"]):
                ok = True
        if ok:
            conflict_hits += 1
    conflict_denom = len(gt["expected_conflicts"])
    conflict_rate = conflict_hits / conflict_denom if conflict_denom else 0

    # --- Completeness (scalar + entities present when expected) ---
    expected_present = scalar_denom + sec_denom + acc_denom + wit_denom
    present_hits = scalar_correct + sec_correct + acc_correct + wit_correct
    completeness = present_hits / expected_present if expected_present else 0

    unresolved = len([c for c in (data.source_conflicts or []) if c.resolution == "unresolved"])

    lines = []
    lines.append("# Accuracy Report — FIR 83/2020 Charge Sheet")
    lines.append("")
    lines.append("Manually verified ground truth: `tests/fixtures/ground_truth_fir83_2020.json`")
    lines.append("")
    lines.append("**Confidence scores from the extractor are NOT accuracy.** Metrics below are measured against ground truth.")
    lines.append("")
    lines.append("## Summary metrics")
    lines.append("")
    lines.append(f"| Metric | Value | Denominator | Correct | Incorrect | Missing |")
    lines.append(f"|---|---:|---:|---:|---:|---:|")
    lines.append(
        f"| Scalar field exact/contains match | {scalar_acc:.3f} | {scalar_denom} | {scalar_correct} | {scalar_incorrect} | {scalar_missing} |"
    )
    lines.append(
        f"| Legal-section entity accuracy | {sec_acc:.3f} | {sec_denom} | {sec_correct} | {sec_denom - sec_correct} | 0 |"
    )
    lines.append(
        f"| Accused entity accuracy | {acc_acc:.3f} | {acc_denom} | {acc_correct} | {acc_incorrect} | {acc_missing} |"
    )
    lines.append(
        f"| Witness entity accuracy | {wit_acc:.3f} | {wit_denom} | {wit_correct} | {wit_denom - wit_correct} | — |"
    )
    lines.append(
        f"| Conflict detection rate | {conflict_rate:.3f} | {conflict_denom} | {conflict_hits} | — | — |"
    )
    lines.append(
        f"| Completeness (all reference items) | {completeness:.3f} | {expected_present} | {present_hits} | — | — |"
    )
    lines.append("")
    lines.append(f"Extractor overall_confidence (NOT accuracy): `{data.overall_confidence}`")
    lines.append(f"Unresolved source conflicts: `{unresolved}`")
    lines.append(f"Unmapped retained fields: `{len(data.unmapped_fields)}`")
    lines.append("")
    lines.append("## Scalar field comparison")
    lines.append("")
    lines.append("| Field | Expected | Got | Result |")
    lines.append("|---|---|---|---|")
    for key, exp, got, status in scalar_rows:
        lines.append(f"| `{key}` | {exp} | {got} | {status} |")
    lines.append("")
    lines.append("## Known source inconsistencies (must not be auto-resolved)")
    lines.append("")
    for c in data.source_conflicts or []:
        lines.append(f"- **{c.field_name}**: values={c.values} — {c.evidence} (resolution={c.resolution})")
    lines.append("")
    lines.append("## Remaining limitations")
    lines.append("")
    lines.append("- Charge-sheet form layouts still vary widely; new templates may need synonym/context extensions.")
    lines.append("- Narrative OCR/text noise can still create residual unmapped KV pairs.")
    lines.append("- Seized-property and lab findings rely on narrative heuristics when form columns are blank.")
    lines.append("- Authorization is configurable (`ENABLE_AUTH`); enable for shared deployments.")
    lines.append("")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nWrote {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
