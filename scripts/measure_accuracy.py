"""Compare extraction against simple ground-truth JSON for sample PDFs.

Reports extraction coverage and field-mapping accuracy separately.
Ground truth lives in tests/fixtures/ground_truth.json (synthetic samples).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.extraction_service import ExtractionService


def norm(v) -> str:
    return " ".join(str(v or "").lower().split())


def main() -> int:
    fixtures = ROOT / "tests" / "fixtures"
    gt_path = fixtures / "ground_truth.json"
    if not gt_path.exists():
        print("ground_truth.json missing")
        return 1
    truth = json.loads(gt_path.read_text(encoding="utf-8"))
    svc = ExtractionService(db=None)

    map_hits = map_total = 0
    extract_hits = extract_total = 0

    for name, expected in truth.items():
        path = fixtures / name
        if not path.exists():
            print("missing pdf", name)
            continue
        data = svc.process(path)
        fmap = {}
        if data.case.district:
            fmap["district"] = data.case.district.original_value
        if data.case.police_station:
            fmap["police_station"] = data.case.police_station.original_value
        if data.case.fir_number:
            fmap["fir_number"] = data.case.fir_number.original_value
        if data.case.charge_sheet_number:
            fmap["charge_sheet_number"] = data.case.charge_sheet_number.original_value

        for key, exp in expected.items():
            extract_total += 1
            got = fmap.get(key)
            if got:
                extract_hits += 1
                map_total += 1
                if norm(exp) in norm(got) or norm(got) in norm(exp):
                    map_hits += 1
                else:
                    print(f"MISS map {name} {key}: expected={exp!r} got={got!r}")
            else:
                map_total += 1
                print(f"MISS extract {name} {key}")

        print(
            f"{name}: accused={len(data.accused_persons)} "
            f"witnesses={len(data.witnesses)} conf={data.overall_confidence}"
        )

    ext_acc = extract_hits / extract_total if extract_total else 0
    map_acc = map_hits / map_total if map_total else 0
    print(f"extraction_coverage={ext_acc:.3f} ({extract_hits}/{extract_total})")
    print(f"field_mapping_accuracy={map_acc:.3f} ({map_hits}/{map_total})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
