"""The final JSON of a chargesheet, built from the same field list the Fields tab shows.

There is no fixed schema: each key is the label exactly as detected in the uploaded document
(so a different template gives different keys), and each value is the complete text of that field
as the Fields tab shows it - a human correction when one was saved, otherwise the extracted value,
with line breaks kept. A field with no value is an empty string; nothing is added, split, shortened
or rewritten.

Two cases need a key the document does not provide:
* text that comes before any label is kept under ``UNLABELLED`` (the same wording the Fields tab uses);
* a label that appears more than once keeps every occurrence: the later ones get " (2)", " (3)", ...
"""

from __future__ import annotations

from typing import Dict, List

from app.schemas.charge_sheet import PdfField

UNLABELLED = "(text before any label)"


def final_value(f: PdfField) -> str:
    """The value the Fields tab shows for this field (same rule as ``shownValue`` in app.js)."""
    if f.corrected_value is not None and f.corrected_value != "":
        return str(f.corrected_value)
    return "" if f.value is None else str(f.value)


def fields_to_json(fields: List[PdfField]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for f in fields:
        base = f.label if f.label else UNLABELLED
        key, n = base, 2
        while key in out:
            key = f"{base} ({n})"
            n += 1
        out[key] = final_value(f)
    return out
