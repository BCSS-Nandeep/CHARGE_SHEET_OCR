"""Inspect FIR 83 PDF text and stored extraction."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "uploads" / "80d251b418464972b530f8b865dd58ee_2021-04-19_13.28.35.818.0.38051334.pdf"
DOC_ID = "445a415f-5f82-4ce1-b847-09d6c974a473"
OUT = ROOT / "tmp_debug_fir83.txt"


def main() -> None:
    lines: list[str] = []
    doc = fitz.open(PDF)
    lines.append(f"pages={len(doc)}")
    for i, page in enumerate(doc):
        text = page.get_text("text")
        lines.append(f"\n==== PAGE {i + 1} chars={len(text)} ====")
        lines.append(text)
    doc.close()

    conn = sqlite3.connect(ROOT / "data" / "charge_sheets.db")
    row = conn.execute(
        "select payload_json from results where document_id=?", (DOC_ID,)
    ).fetchone()
    payload = json.loads(row[0])
    lines.append("\n==== CASE ====")
    lines.append(json.dumps(payload.get("case"), indent=2, default=str))
    lines.append("\n==== ACCUSED ====")
    lines.append(json.dumps(payload.get("accused_persons"), indent=2, default=str))
    lines.append("\n==== WITNESSES ====")
    lines.append(json.dumps(payload.get("witnesses"), indent=2, default=str))
    lines.append("\n==== SECTIONS ====")
    lines.append(json.dumps(payload.get("acts_and_sections"), indent=2, default=str))
    lines.append("\n==== UNMAPPED ====")
    for u in payload.get("unmapped_fields") or []:
        lines.append(
            f"{u.get('canonical_name')} | {u.get('original_label')!r} => {(u.get('original_value') or '')[:120]}"
        )
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
