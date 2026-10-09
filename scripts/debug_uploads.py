"""Dump text samples from uploaded PDFs for diagnosis (no PII logging beyond structure)."""

from __future__ import annotations

from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tmp_debug_uploads.txt"


def main() -> None:
    lines: list[str] = []
    for pdf in sorted((ROOT / "uploads").glob("*.pdf")):
        doc = fitz.open(pdf)
        lines.append(f"\n##### {pdf.name} pages={len(doc)}")
        for i, page in enumerate(doc):
            text = page.get_text("text") or ""
            lines.append(f"--- page {i+1} chars={len(text.strip())} ---")
            lines.append(text[:2500])
        doc.close()
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
