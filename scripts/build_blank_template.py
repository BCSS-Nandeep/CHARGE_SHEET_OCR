"""Regenerate the blank standard IR template from the schema.

    python scripts/build_blank_template.py

The file is produced by the same writer that renders filled reports, so it can
never drift from the schema. Re-run it after editing app/ir_standard/schema.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ir_standard.docx_writer import render_docx  # noqa: E402

TARGET = ROOT / "app" / "ir_standard" / "templates" / "IR_Standard_Blank.docx"


def main() -> None:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    render_docx(None, TARGET)
    print(f"wrote {TARGET.relative_to(ROOT)} ({TARGET.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
