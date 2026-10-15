"""ASGI entry point of the unified application.

    python -m uvicorn unified.app:app --host 127.0.0.1 --port 8000        (or .\\run_unified.ps1)

Imports the existing IR_OCR application object (``app.main:app``, unchanged) as the host and puts the unified gateway in
front of it. The charge-sheet worker (the unmodified CHARGE_SHEET_OCR snapshot) is started on demand as its own process.
"""

from .factory import create_gateway

app = create_gateway()
