"""Unified police-document OCR application (MERGE_MASTER).

One front door in front of two unchanged extraction services:

* ``interrogation_report`` -> the IR_OCR application in this repository (``app/``), called in-process through its own
  upload route and repositories;
* ``charge_sheet``         -> CHARGE_SHEET_OCR, an unmodified snapshot in ``vendor/charge_sheet_ocr/`` that runs as its
  own process (its ``app`` package has the same name as IR_OCR's, so the two can never share an interpreter).

Nothing in ``app/`` or ``vendor/charge_sheet_ocr/`` is edited; everything here attaches from the outside.
See ``docs/UNIFIED_ARCHITECTURE.md``.
"""

UNIFIED_VERSION = "1.0.0"
# Version of the combined ``report_result.json`` envelope. It is independent of the native OCR schema versions
# (``results.schema_version``), which are copied from each service and never changed. 1.x was the IR-only envelope
# of the person-image integration; 2.0.0 adds the document type, classification, review and provenance blocks.
ENVELOPE_VERSION = "2.0.0"

CHARGE_SHEET = "charge_sheet"
INTERROGATION_REPORT = "interrogation_report"
UNKNOWN = "unknown"
DOCUMENT_TYPES = (CHARGE_SHEET, INTERROGATION_REPORT)
