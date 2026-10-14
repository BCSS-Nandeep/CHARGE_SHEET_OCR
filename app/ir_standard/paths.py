"""Flat view of the standard IR for the existing field list / correction flow.

Every value of the standard JSON has a stable dotted id
(``s03_physical_features.height``, ``s05_family_history.members.0.name``).
``flatten`` lists them for the UI; ``set_value`` writes a reviewer's correction
back into the JSON, snapping choice values to the template's options.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from app.ir_standard.schema import MULTI, SECTION_BY_KEY, SECTIONS, FieldSpec, SectionSpec, TableSpec
from app.ir_standard.validator import blank_row, snap_option


@dataclass
class FlatField:
    id: str
    section: int
    serial: Optional[str]
    label: str
    value: str
    kind: str
    flagged: bool
    long_text: bool = False
    section_title: str = ""  # "3. PHYSICAL FEATURES": shown above the section's fields
    group: Optional[str] = None  # sub-heading inside the section: a table's title, or a block of columns


def value_to_str(spec: FieldSpec, value: Any) -> str:
    if value is None:
        return ""
    if spec.kind == "text":
        return str(value)
    sel = value.get("selected")
    text = ", ".join(sel) if isinstance(sel, list) else (sel or "")
    other = value.get("other")
    return text if text else (other or "")


def field_group(spec: SectionSpec, f: FieldSpec) -> Optional[str]:
    """Sub-heading a field is listed under (None: directly under the section title)."""
    if spec.layout == "physical" and f.column_no is not None:
        if f.column_no <= 12:
            return "Build and features (columns 1–12)"
        return "Identification marks and peculiarities (columns 13–20)"
    return None


def flatten(standard: Dict[str, Any]) -> List[FlatField]:
    flagged: Set[str] = {f.get("path", "") for f in standard.get("review_flags", [])}
    flagged_sections = {f["section"] for f in standard.get("review_flags", []) if f.get("code") == "section_not_found"}
    out: List[FlatField] = []
    for spec in SECTIONS:
        data = standard["sections"].get(spec.key, {})
        for f in spec.fields:
            fid = f"{spec.key}.{f.key}"
            out.append(FlatField(
                fid, spec.number, f"({f.column_no})" if f.column_no else None, f.label,
                value_to_str(f, data.get(f.key)), f.kind,
                fid in flagged or spec.key in flagged_sections,
                long_text=f.key in ("part1_facts", "part3_facts", "part1_accused", "mo_description", "notes", "links"),
                section_title=spec.heading, group=field_group(spec, f),
            ))
        for t in spec.tables:
            for i, row in enumerate(data.get(t.key) or []):
                row_label = row.get(t.columns[0].key) if t.fixed_rows else f"Row {i + 1}"
                for c in t.columns[1 if t.fixed_rows else 0:]:
                    fid = f"{spec.key}.{t.key}.{i}.{c.key}"
                    out.append(FlatField(
                        fid, spec.number, None, f"{t.title} – {row_label} – {c.label}",
                        value_to_str(c, row.get(c.key)), c.kind, fid in flagged,
                        section_title=spec.heading, group=t.title,
                    ))
    return out


def _spec_for(parts: List[str]) -> Optional[tuple]:
    """(section, container-dict path, field spec, table spec|None) for an id, or None."""
    section = SECTION_BY_KEY.get(parts[0])
    if section is None:
        return None
    if len(parts) == 2:
        spec = next((f for f in section.fields if f.key == parts[1]), None)
        return (section, None, spec, None) if spec else None
    if len(parts) == 4 and parts[2].isdigit():
        table = next((t for t in section.tables if t.key == parts[1]), None)
        spec = next((c for c in table.columns if c.key == parts[3]), None) if table else None
        return (section, int(parts[2]), spec, table) if spec else None
    return None


def set_value(standard: Dict[str, Any], field_id: str, text: Optional[str]) -> bool:
    """Write ``text`` into the standard JSON; False when the id is unknown."""
    found = _spec_for(field_id.split("."))
    if not found:
        return False
    section, row_index, spec, table = found
    parts = field_id.split(".")
    holder = standard["sections"].setdefault(section.key, {})
    if table is not None:
        rows = holder.setdefault(table.key, [])
        if row_index >= table.max_rows:
            return False
        while len(rows) <= row_index:  # a reviewer filling a blank / new row of the template
            rows.append(blank_row(table))
        holder, key = rows[row_index], spec.key
    else:
        key = spec.key
    text = (text or "").strip()
    if spec.kind == "text":
        holder[key] = text or None
        return True
    items = [p for p in re.split(r"\s*[;,]\s*", text) if p] if spec.kind == MULTI else ([text] if text else [])
    selected, other = [], []
    for item in items:
        snapped = snap_option(item, spec.options)
        (selected if snapped else other).append(snapped or item)
    selected = list(dict.fromkeys(selected))
    holder[key] = {
        "selected": selected if spec.kind == MULTI else (selected[0] if selected else None),
        "evidence": "written" if (selected or other) else None,
        "other": ", ".join(other) or None,
    }
    return True


def apply_overrides(standard: Dict[str, Any], overrides: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """Copy of ``standard`` with reviewer corrections applied (original is untouched)."""
    out = copy.deepcopy(standard)
    for fid, text in overrides.items():
        set_value(out, fid, text)
    return out
