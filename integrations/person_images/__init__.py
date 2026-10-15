"""Person-image extraction for IR_OCR, added next to the OCR application (see README.md).

Nothing here is imported by the OCR code. ``attach(app)`` (in ``wiring.py``) adds the routes, the upload hook and
the UI tab to an existing FastAPI application; ``integrations.person_images.app`` does that for the real one.
"""

INTEGRATION_VERSION = "1.0.0"
REPORT_RESULT_SCHEMA_VERSION = "1.0.0"
