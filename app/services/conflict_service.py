"""Stage: detect conflicting source values without silently resolving them.

Conflicts are attached to the exact field they concern (by canonical name),
never to every person of a kind.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence, Tuple

from app.schemas.charge_sheet import ChargeSheetData, SourceConflict, ValidationStatus
from app.services.entity_extractor import PersonEntity, PersonMention
from app.services.legal_section_parser import section_key
from app.services.text_normalize import compact


def _sim(a: str, b: str) -> float:
    a, b = compact(a), compact(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _parent_name(value: Optional[str]) -> str:
    """'S/o John Doe' → 'John Doe'."""
    return re.sub(r"(?i)^(?:[sdwch]\s*/\s*[o.]|son of|daughter of|wife of|husband of)\s*", "", value or "").strip()


def _tokens(name: str) -> set:
    return {t.lower() for t in re.findall(r"[A-Za-z]{2,}", name or "")}


PARENT_MATCH = 0.8
NAME_MATCH = 0.8
NAME_ONLY_MATCH = 0.9


def _match_score(person: PersonEntity, mention: PersonMention) -> float:
    """0 when the mention is not this person; otherwise a similarity score.

    Both the person's name and parent must agree closely. Many names share long
    common endings and surname suffixes, so loose thresholds link the wrong
    people and invent conflicts.
    """
    if not person.name or not mention.name:
        return 0.0
    name_sim = _sim(person.name, mention.name)
    shortened = bool(_tokens(mention.name)) and (
        _tokens(mention.name) <= _tokens(person.name) or _tokens(person.name) <= _tokens(mention.name)
    )
    parent = _parent_name(person.parent_or_spouse)
    if parent and mention.parent:
        parent_sim = _sim(parent, mention.parent)
        if parent_sim >= PARENT_MATCH and (name_sim >= NAME_MATCH or shortened):
            return name_sim + parent_sim
        return 0.0
    return name_sim if name_sim >= NAME_ONLY_MATCH else 0.0


def assign_mentions(
    people: Sequence[PersonEntity], mentions: Sequence[PersonMention]
) -> Dict[int, List[PersonMention]]:
    """Each mention goes to the single best-matching person (if any)."""
    out: Dict[int, List[PersonMention]] = {}
    for mention in mentions:
        scored = [(_match_score(p, mention), i) for i, p in enumerate(people)]
        best = max(scored, default=(0.0, -1))
        if best[0] > 0:
            out.setdefault(best[1], []).append(mention)
    return out


def _distinct(values: List[Tuple[str, Optional[int]]]) -> Tuple[List[str], List[int]]:
    seen: Dict[str, str] = {}
    pages: List[int] = []
    for value, page in values:
        key = compact(value)
        if not key:
            continue
        if key not in seen:
            seen[key] = value
        if page and page not in pages:
            pages.append(page)
    return list(seen.values()), sorted(pages)


def detect_person_conflicts(
    people: Sequence[PersonEntity],
    mentions: Sequence[PersonMention],
    prefix: str,
) -> List[SourceConflict]:
    """Name / age / parentage disagreements between a list entry and narrative mentions."""
    conflicts: List[SourceConflict] = []
    for idx, matched in assign_mentions(people, mentions).items():
        person = people[idx]
        checks = [
            ("name", person.name, [m.name for m in matched], "Name spelled differently"),
            ("age", person.age, [m.age for m in matched], "Different ages given"),
            (
                "parent_or_spouse",
                _parent_name(person.parent_or_spouse),
                [m.parent for m in matched],
                "Parent/spouse name spelled differently",
            ),
        ]
        for attr, primary, others, evidence in checks:
            if not primary:
                continue
            vals = [(primary, person.source_page)] + [
                (o, m.page)
                for o, m in zip(others, matched)
                # A shortened reference ("Doe" for "John Doe") or a mention carrying extra
                # leading words is the same spelling, not a variant.
                if o and not (attr == "name" and (_tokens(o) <= _tokens(primary) or _tokens(primary) <= _tokens(o)))
            ]
            distinct, pages = _distinct(vals)
            if len(distinct) > 1:
                conflicts.append(
                    SourceConflict(
                        field_name=f"{prefix}[{idx}].{attr}",
                        values=distinct,
                        source_pages=pages,
                        evidence=f"{evidence} for the same person in the source "
                        f"(list entry vs. narrative: {matched[0].text[:120]!r})",
                        resolution="unresolved",
                    )
                )
    return conflicts


def apply_conflicts_to_fields(data: ChargeSheetData, conflicts: List[SourceConflict]) -> ChargeSheetData:
    """Record conflicts and mark only the affected fields as CONFLICTING."""
    if not conflicts:
        return data
    data.source_conflicts = list(data.source_conflicts or []) + conflicts
    fmap = data.field_map()
    for conflict in conflicts:
        targets = []
        m = re.match(r"legal\.act\[(.+)\]$", conflict.field_name)
        if m:
            key = m.group(1)
            for sec in list(data.acts_and_sections) + list(data.other_legal_references):
                if sec.section and sec.act and section_key(str(sec.section.original_value)) == key:
                    targets.append(sec.act)
        elif conflict.field_name in fmap:
            targets.append(fmap[conflict.field_name])
        for f in targets:
            own = compact(_parent_name(str(f.original_value or "")))
            f.validation_status = ValidationStatus.CONFLICTING
            f.alternate_values = [v for v in conflict.values if compact(v) != own and v not in f.alternate_values] + [
                v for v in f.alternate_values if compact(v) != own
            ]
            f.alternate_values = list(dict.fromkeys(f.alternate_values))
            note = f"Unresolved source conflict: {conflict.values}"
            f.notes = f"{f.notes}; {note}" if f.notes else note
    data.requires_human_review = True
    data.extraction_warnings = list(data.extraction_warnings or []) + [
        f"{len(conflicts)} source conflict(s) detected and left unresolved"
    ]
    return data
