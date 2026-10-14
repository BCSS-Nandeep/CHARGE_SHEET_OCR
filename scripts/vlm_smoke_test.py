"""Smoke-test the local VLM server through the app's own client.

    python scripts/vlm_smoke_test.py path\\to\\file.pdf [page]      # a PDF page, rendered at VLM_DPI
    python scripts/vlm_smoke_test.py path\\to\\page.png

Reads LLM_* / VLM_* settings from .env like the app does. Prints the transcription the
pipeline would receive, the time taken, and the server's reported model. Use a non-sensitive
page: the text is printed to the terminal.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.ir_standard.mapper import PAGE_PROMPT  # noqa: E402
from app.ir_standard.vlm_client import VlmClient, VlmError  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    settings = get_settings()
    if path.suffix.lower() == ".pdf":
        page = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        with fitz.open(path) as doc:
            zoom = settings.vlm_dpi / 72.0
            png = doc[page - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("png")
    else:
        png = path.read_bytes()
    print(f"endpoint={settings.llm_base_url} model={settings.llm_model} image={len(png) / 1024:.0f} KiB")
    try:
        client = VlmClient(settings)
        if not client.is_up():
            print("server not reachable")
            return 1
        t0 = time.monotonic()
        text = client.transcribe_png(png, PAGE_PROMPT)
    except VlmError as exc:
        print(f"VLM error: {exc}")
        return 1
    print(f"--- transcription ({time.monotonic() - t0:.1f}s, {len(text)} chars) ---")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
