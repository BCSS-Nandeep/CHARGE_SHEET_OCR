"""The report as the standard template, in JSON: section headings, then fields and table columns, with values.

This is what the UI shows, in the same order and with the same labels and groups; it is built from the same
flat field list (``paths.flatten``), so the two cannot disagree. Every section, field and table column of the
template is always present: a value the report does not have is an empty string (``[]`` for several-choice
fields), never a missing key. The model's evidence and raw lines are not mixed in; they stay in the audit JSON.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.ir_standard.paths import field_group, value_to_str
from app.ir_standard.schema import MULTI, SCHEMA_VERSION, SECTIONS, FieldSpec, SectionSpec

TEMPLATE_JSON_FORMAT = "ir-template-json-1.0"


def _value(spec: FieldSpec, raw: Any) -> Any:
    """Plain value: text as written; a choice as its selected option (or the text written when no option
    matches); several-choice as a list."""
    if spec.kind == MULTI:
        if not isinstance(raw, dict):
            return []
        items = list(raw.get("selected") or [])
        if raw.get("other"):
            items.append(raw["other"])
        return items
    return value_to_str(spec, raw)


def _review(review: Dict[str, Dict[str, Optional[str]]], path: str) -> Dict[str, Any]:
    info = review.get(path) or {}
    out: Dict[str, Any] = {"review": info.get("status") or "unreviewed"}
    if info.get("note"):
        out["note"] = info["note"]
    return out


def build_template_json(
    standard: Dict[str, Any],
    review: Optional[Dict[str, Dict[str, Optional[str]]]] = None,
    report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """``standard``: the standard JSON (corrections already applied). ``review``: field id -> {status, note}
    (from the stored field list). ``report``: status block (draft / verified) for the header."""
    review = review or {}
    absent = {f["section"] for f in standard.get("review_flags", [])
              if f.get("code") in ("section_not_found", "mapping_failed")}
    sections: List[Dict[str, Any]] = []
    for spec in SECTIONS:
        data = standard["sections"].get(spec.key, {})
        groups: List[Dict[str, Any]] = []
        for f in spec.fields:
            heading = field_group(spec, f)
            if not groups or groups[-1]["heading"] != heading:
                groups.append({"heading": heading, "fields": []})
            item: Dict[str, Any] = {"key": f.key, "label": f.label}
            if f.column_no is not None:
                item["column"] = f.column_no
            item["value"] = _value(f, data.get(f.key))
            item.update(_review(review, f"{spec.key}.{f.key}"))
            groups[-1]["fields"].append(item)
        tables: List[Dict[str, Any]] = []
        for t in spec.tables:
            rows = []
            for i, row in enumerate(data.get(t.key) or []):
                cells: Dict[str, Any] = {}
                cell_review: Dict[str, Any] = {}
                for c in t.columns:
                    cells[c.key] = _value(c, row.get(c.key))
                    info = _review(review, f"{spec.key}.{t.key}.{i}.{c.key}")
                    if info["review"] != "unreviewed" or "note" in info:
                        cell_review[c.key] = info
                entry: Dict[str, Any] = {"cells": cells}
                if cell_review:
                    entry["review"] = cell_review
                rows.append(entry)
            tables.append({
                "key": t.key, "title": t.title,
                "columns": [{"key": c.key, "label": c.label} for c in t.columns],
                "rows": rows,
            })
        section: Dict[str, Any] = {
            "number": spec.number, "key": spec.key, "title": spec.title,
            "heading": spec.heading, "in_report": spec.key not in absent,
        }
        if groups:
            section["groups"] = groups
        if tables:
            section["tables"] = tables
        sections.append(section)
    return {
        "format": TEMPLATE_JSON_FORMAT,
        "template": SCHEMA_VERSION,
        "report": report or {},
        "sections": sections,
    }
