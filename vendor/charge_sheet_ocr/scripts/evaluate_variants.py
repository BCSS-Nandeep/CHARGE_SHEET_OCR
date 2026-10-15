"""Same-template / different-data robustness check for the known templates.

    python scripts/evaluate_variants.py            # 25 variants per template
    python scripts/evaluate_variants.py --n 100    # more

For every template that has a variant spec in tests/fixtures/private/variant_specs/
(and whose source PDF is available), generate N variants with different names,
places, ages, phone numbers, FIR numbers, dates and sections, plus added/dropped
witnesses and accused, run the pipeline and compare with each variant's ground truth.

Prints a summary with field names only; value-level details go to
tests/fixtures/private/VARIANT_DETAILS.md (gitignored).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=25)
    parser.add_argument("--template", default=None)
    parser.add_argument("--details", default=str(ROOT / "tests" / "fixtures" / "private" / "VARIANT_DETAILS.md"))
    args = parser.parse_args()

    os.environ["OCR_ENABLED"] = "true"  # scanned template replays cached OCR boxes
    from app.config import get_settings

    get_settings.cache_clear()
    import logging

    logging.disable(logging.WARNING)
    from evaluation.variant_runner import load_benches, run_variant

    benches = [b for b in load_benches() if not args.template or b.template_id == args.template]
    if not benches:
        print("No variant specs / source PDFs available.")
        return 1
    details = ["# Variant evaluation details (PRIVATE)", ""]
    failed_total = 0
    for bench in benches:
        passed = 0
        problems: Counter = Counter()
        for seed in range(1, args.n + 1):
            outcome = run_variant(bench, seed)
            if outcome.unexpected:
                problems.update(outcome.unexpected)
                details.append(f"## {bench.template_id} seed {seed} — {outcome.description}")
                known = set(bench.ground_truth.data.get("known_gaps", []))
                details += [f"- {m}" for m in outcome.detailed.mismatches if not any(m.startswith(k) for k in known)]
                details.append("")
            else:
                passed += 1
        failed_total += args.n - passed
        print(f"{bench.template_id:28s} {passed}/{args.n} variants fully correct")
        for msg, count in problems.most_common(12):
            print(f"    {count:3d}x  {msg}")
    Path(args.details).write_text("\n".join(details), encoding="utf-8")
    print(f"\nDetails: {args.details}")
    return 0 if failed_total == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
