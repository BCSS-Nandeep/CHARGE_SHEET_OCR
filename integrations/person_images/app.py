"""ASGI entry point of the integrated IR_OCR application.

    python -m uvicorn integrations.person_images.app:app --host 127.0.0.1 --port 8000

It imports the existing application object (``app.main:app``, unchanged) and attaches the person-image routes, the
upload hook and the Person Images tab to it. Starting ``app.main:app`` directly still works exactly as before, without
the image features.
"""

from app.main import app as _ocr_app

from .wiring import attach

app = attach(_ocr_app)
