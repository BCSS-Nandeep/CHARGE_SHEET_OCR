"""The one final structured JSON of a chargesheet: the same data the review UI shows and edits.

For every field: ``value`` is the final value (a human correction if there is one, otherwise the
machine value; ``null`` when the field was not found or is still conflicting), next to the
``machine_value`` it came from, its status, source page / text and review flags. Nothing is
marked verified except by the document's own human verification.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.schemas.structured import SECTIONS, StructuredData, StructuredValue


def _final(v: StructuredValue) -> Dict[str, Any]:
    corrected = v.corrected_value is not None and v.corrected_value != ""
    return {
        "value": v.corrected_value if corrected else v.value,
        "machine_value": v.value,
        "status": v.status.value,
        "human_corrected": corrected,
        "review_status": v.review_status,
        "needs_review": bool(v.needs_review and not corrected),
        "source_page": v.source_page,
        "source_text": v.source_text,
        "pages": v.pages,
        "candidates": [c.model_dump(mode="json") for c in v.candidates] if v.status.value == "conflicting" else [],
        "note": v.note,
    }


def final_structured_json(
    s: StructuredData,
    *,
    document_id: str,
    file_name: str,
    verified: bool,
    verified_by: Optional[str],
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "document_id": document_id,
        "file_name": file_name,
        "schema_version": s.schema_version,
        "document_status": s.document_status.value,
        "verification": {"verified": verified, "verified_by": verified_by},
        "mapping_model": s.mapping_model,
        "text_engines": s.transcribe_engine,
    }
    for sec in SECTIONS:
        if sec.repeated:
            out[sec.key] = [
                {"pages": rec.pages, "fields": {k: _final(v) for k, v in rec.fields.items()}}
                for rec in s.lists.get(sec.key, [])
            ]
        else:
            out[sec.key] = {k: _final(v) for k, v in s.single.get(sec.key, {}).items()}
    out["other_details"] = [d.model_dump(mode="json") for d in s.other_details]
    out["pages"] = [p.model_dump(mode="json") for p in s.pages]
    out["validation"] = s.validation.model_dump(mode="json")
    out["warnings"] = s.warnings
    return out
