"""Stage: detect conflicting extracted values without silently resolving them."""

from __future__ import annotations

import re
from typing import List, Optional

from app.schemas.charge_sheet import (
    ChargeSheetData,
    ExtractedField,
    SourceConflict,
    ValidationStatus,
)
from app.services.entity_extractor import PersonEntity


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


def detect_person_conflicts(
    primary: PersonEntity,
    cross_doc: Optional[PersonEntity] = None,
) -> List[SourceConflict]:
    conflicts: List[SourceConflict] = []
    ages = list(primary.age_mentions)
    variants = list(primary.name_variants)
    if cross_doc:
        ages.extend(cross_doc.age_mentions)
        variants.extend(cross_doc.name_variants)

    uniq_ages = []
    for a in ages:
        if a not in uniq_ages:
            uniq_ages.append(a)
    if len(uniq_ages) > 1:
        conflicts.append(
            SourceConflict(
                field_name=f"accused.age:{primary.name or 'unknown'}",
                values=uniq_ages,
                source_pages=[primary.source_page] if primary.source_page else [],
                evidence="Multiple ages found for the same accused across the document",
                resolution="unresolved",
            )
        )

    # Spelling variants (different normalized forms)
    uniq_vars = []
    seen = set()
    for v in variants:
        key = _norm_name(v)
        if not key or key in seen:
            continue
        seen.add(key)
        uniq_vars.append(v)
    if len(uniq_vars) > 1:
        conflicts.append(
            SourceConflict(
                field_name=f"accused.name:{primary.name or 'unknown'}",
                values=uniq_vars,
                source_pages=[primary.source_page] if primary.source_page else [],
                evidence="Name spelling variations found in source text; not auto-resolved",
                resolution="unresolved",
            )
        )
    return conflicts


def apply_conflicts_to_fields(data: ChargeSheetData, conflicts: List[SourceConflict]) -> ChargeSheetData:
    data.source_conflicts = list(data.source_conflicts or []) + conflicts
    # Mark related extracted fields as conflicting where possible
    for conflict in conflicts:
        if "accused.age" in conflict.field_name:
            for acc in data.accused_persons:
                if acc.age:
                    acc.age.validation_status = ValidationStatus.CONFLICTING
                    acc.age.alternate_values = [
                        v for v in conflict.values if v != (acc.age.original_value or "")
                    ]
                    note = f" unresolved age conflict in source: {conflict.values}"
                    acc.age.notes = ((acc.age.notes or "") + note).strip()
        if "accused.name" in conflict.field_name:
            for acc in data.accused_persons:
                if acc.name:
                    acc.name.validation_status = ValidationStatus.CONFLICTING
                    acc.name.alternate_values = [
                        v for v in conflict.values if v.lower() != (acc.name.original_value or "").lower()
                    ]
                    note = f" unresolved name spelling variants in source: {conflict.values}"
                    acc.name.notes = ((acc.name.notes or "") + note).strip()
    if conflicts:
        data.requires_human_review = True
        data.extraction_warnings = list(data.extraction_warnings or [])
        data.extraction_warnings.append(
            f"{len(conflicts)} source conflict(s) detected and left unresolved"
        )
    return data


def detect_scalar_conflicts(fields: List[ExtractedField]) -> List[SourceConflict]:
    """If same canonical appears with different originals in a flat list."""
    by_name = {}
    for f in fields:
        by_name.setdefault(f.canonical_name, []).append(f)
    conflicts: List[SourceConflict] = []
    for name, items in by_name.items():
        vals = []
        pages = []
        for it in items:
            v = (it.original_value or "").strip()
            if not v:
                continue
            if v not in vals:
                vals.append(v)
                if it.source_page:
                    pages.append(it.source_page)
        if len(vals) > 1:
            conflicts.append(
                SourceConflict(
                    field_name=name,
                    values=vals,
                    source_pages=pages,
                    evidence="Conflicting extracted values for the same canonical field",
                    resolution="unresolved",
                )
            )
            for it in items:
                it.validation_status = ValidationStatus.CONFLICTING
    return conflicts
