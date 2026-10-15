"""Helpers shared by the unified-layer tests: synthetic documents, upload/poll shortcuts, a minimal JSON-schema check."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

from tests.person_images.helpers import RED, make_docx, make_pdf, make_png  # noqa: F401  (re-exported)
from tests.unified_layer.conftest import wait_for

SCHEMA = Path(__file__).resolve().parents[2] / "unified" / "report_result.schema.json"

CS_TEXT = ("CHARGE SHEET (Under Section 173 Cr.P.C.) Final Report / Chargesheet No. 12/2025 "
           "Particulars of the accused List of witnesses Property seized Acts and sections")
FIR_TEXT = "FIRST INFORMATION REPORT (Under section 173 and 176 BNSS) District Medak PS Havelighanapur FIR No. 185/2021 " * 3
VAGUE_TEXT = "Memo regarding office stationery supply. Please arrange the items by Monday. " * 4


def ir_docx(tmp_path: Path, images=(), name="ir_report.docx") -> bytes:
    return make_docx(tmp_path / name, images=images).read_bytes()


def cs_pdf(tmp_path: Path, images=(), text=CS_TEXT, name="cs.pdf") -> bytes:
    return make_pdf(tmp_path / name, images=images, text=text).read_bytes()


def upload(client, name: str, data: bytes, document_type: str | None = None, headers=None):
    form = {"document_type": document_type} if document_type else {}
    return client.post("/api/v1/documents/upload", files={"file": (name, data, "application/octet-stream")},
                       data=form, headers=headers or {})


def report(client, doc_id: str) -> dict:
    return client.get(f"/api/v1/documents/{doc_id}/report_result").json()


def wait_report(client, doc_id: str, **expected) -> dict:
    """Poll the combined JSON until processing matches (keys of ``processing``)."""
    def done():
        data = report(client, doc_id)
        p = data["processing"]
        return data if all(p.get(k) == v for k, v in expected.items()) else None

    return wait_for(done)


def validate(instance: Any, schema: dict, path: str = "$") -> List[str]:
    """A small subset of JSON Schema (type, required, properties, enum, const, items) - enough to hold the envelope."""
    errors: List[str] = []
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool,
             "null": type(None)}
    t = schema.get("type")
    if t is not None:
        names = t if isinstance(t, list) else [t]
        ok = any(isinstance(instance, types[n]) and not (n in ("integer", "number") and isinstance(instance, bool))
                 for n in names)
        if not ok:
            return [f"{path}: expected {names}, got {type(instance).__name__}"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected {schema['const']!r}, got {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in {schema['enum']}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing {key}")
        for key, sub in schema.get("properties", {}).items():
            if key in instance:
                errors += validate(instance[key], sub, f"{path}.{key}")
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errors += validate(item, schema["items"], f"{path}[{i}]")
    return errors


def envelope_errors(data: dict) -> List[str]:
    return validate(data, json.loads(SCHEMA.read_text(encoding="utf-8")))
