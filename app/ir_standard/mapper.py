"""Map a report (as markdown) onto the standard template with the VLM.

One request per section keeps each prompt small enough for a CPU-only server
and keeps a failure local: a section that cannot be mapped is left empty and
flagged, the rest of the report is unaffected. The model only proposes values;
``validator.build_standard`` decides what is valid.
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

from app.ir_standard.evidence import EvidenceIndex, html_tables_to_markdown
from app.ir_standard.schema import MULTI, SECTIONS, SINGLE, FieldSpec, SectionSpec, TableSpec
from app.ir_standard.sectioner import split_sections
from app.ir_standard.validator import build_standard
from app.ir_standard.vlm_client import VlmClient, VlmError
from app.logging_utils import get_logger

logger = get_logger(__name__)

MAX_CHUNK_CHARS = 14000

PAGE_PROMPT = (
    "Transcribe this page to markdown exactly as printed. Keep every table as a markdown table, "
    "one row per line. Put ☑ immediately before every option that is ticked or has a checked box, and "
    "☐ before every option with an empty box. Wrap bold text in **double asterisks**. "
    "Keep handwritten or typed notes such as 'Yes' next to the option they stand beside. "
    "Do not add, correct, translate or summarise anything."
)

SYSTEM_PROMPT = (
    "You map text from one section of an Interrogation Report onto a fixed template. "
    "Answer with one JSON object only: no commentary, no markdown fences."
)

RULES = """Rules:
- Return ONE JSON object with ONLY the fields of FIELDS whose value is really written in REPORT TEXT. Leave out every other key. Use only the keys listed under FIELDS.
- The report text may be a table, a list of options under a label, "Label: value" lines, or running sentences. Lines under "COLUMN <name>" or "ROW <name>" are the options and values printed in that one column or row; never take a value from a different column.
- A ticked box (☑) means the option is selected. An empty box (☐) with plain text means it is NOT selected. Bold text or a written 'Yes' beside an option are weaker signals: report such an option only when no option of that field is ticked, and say so in "evidence".
- If nothing is marked, leave the field out. Do not pick an option that has no mark just because it comes first.
- A choice field is answered as {"selected": <option copied exactly as listed under OPTIONS>, "evidence": "tick"|"bold"|"yes_text"|"written"} (selected is a list for "any of" fields). If the report names something not listed, use {"other": "<the report's text>"}.
- Free-text fields: copy the report's text verbatim from that field's own column or row. Never invent or correct values.
- Tables: a list of row objects, one per filled row; skip rows with no content. If the table has no filled rows, leave the key out.
- NEVER make up, guess or complete anything. If a field, option or table is not in REPORT TEXT, or is blank, leave it out. Do not write placeholder text such as "N/A", "Member 1" or a sample address.
- The report may use a different layout from the standard form, or leave out whole fields. Fill a field only from text that is really there."""


# ---------------------------------------------------------------------------
# prompt building
# ---------------------------------------------------------------------------

def _field_template(f: FieldSpec):
    """Every value is empty: nothing here can be mistaken for data to copy. (Kept for tests / documentation;
    the prompt lists FIELDS instead, because a small model copies an empty skeleton back as-is.)"""
    if f.kind == "text":
        return None
    return {"selected": [] if f.kind == MULTI else None, "evidence": None, "other": None}


def _table_template(t: TableSpec):
    return [{c.key: _field_template(c) for c in t.columns}]


def section_template(spec: SectionSpec) -> dict:
    out = {f.key: _field_template(f) for f in spec.fields}
    for t in spec.tables:
        out[t.key] = _table_template(t)
    return out


def _options_block(spec: SectionSpec) -> str:
    lines = []
    for f in spec.fields:
        if f.kind != "text":
            lines.append(f"- {f.key} ({'any of' if f.kind == MULTI else 'one of'}): " + " | ".join(f.options))
    for t in spec.tables:
        if t.fixed_rows:
            lines.append(f"- {t.key}.{t.columns[0].key} (one row per relation present in the report): " + " | ".join(t.fixed_rows))
        for c in t.columns:
            if c.kind != "text":
                lines.append(f"- {t.key}.{c.key} (one of): " + " | ".join(c.options))
    return "\n".join(lines)


def _fields_block(spec: SectionSpec) -> str:
    """The section's fields as plain text: key, printed label, kind. No values anywhere."""
    lines = []
    for f in spec.fields:
        kind = "text" if f.kind == "text" else ("choice, several allowed" if f.kind == MULTI else "choice")
        lines.append(f"- {f.key}: {f.label} [{kind}]")
    for t in spec.tables:
        lines.append(f"- {t.key}: table \"{t.title}\"; each row has the keys " + ", ".join(c.key for c in t.columns))
    return "\n".join(lines)


def build_prompt(spec: SectionSpec, chunk: str) -> str:
    hints = "\n".join(f"- {h}" for h in spec.hints)
    options = _options_block(spec)
    return (
        f"SECTION: {spec.heading}\n\n{RULES}\n"
        + (f"{hints}\n" if hints else "")
        + f"\nFIELDS (key: label [kind]):\n{_fields_block(spec)}\n"
        + (f"\nOPTIONS (choice fields only; copy an option exactly):\n{options}\n" if options else "")
        + f"\nREPORT TEXT:\n{chunk}\n\nJSON (only the fields found in the report text):"
    )


# ---------------------------------------------------------------------------
# response parsing
# ---------------------------------------------------------------------------

def extract_json(text: str) -> Optional[dict]:
    """First JSON object in ``text`` (tolerates ```json fences and chatter around it)."""
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start: i + 1])
                        return obj if isinstance(obj, dict) else None
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


# ---------------------------------------------------------------------------
# narrative (Part-I / Part-III): copied by Python, never regenerated by the model
# ---------------------------------------------------------------------------

def parse_narrative(chunk: str) -> dict:
    lines = [re.sub(r"\*\*|^\W+", "", ln).strip() for ln in chunk.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("|")]
    long = [ln for ln in lines if len(ln) > 200]
    out: Dict[str, Optional[str]] = {
        "part1_facts": long[0] if long else None,
        "part3_facts": long[1] if len(long) > 1 else None,
    }
    for ln in lines:
        m = re.match(r"^date\s*[:\-]\s*(.+)$", ln, re.IGNORECASE)
        if m and not out.get("report_date"):
            out["report_date"] = m.group(1).strip()
        if re.match(r"^basing on the above", ln, re.IGNORECASE):
            out["part3_registration"] = ln
    for i, ln in enumerate(lines):
        if re.match(r"^sub[\s-]*inspector|^inspector|^circle inspector", ln, re.IGNORECASE):
            out["officer"] = ln.rstrip(", ")
            if i + 1 < len(lines) and len(lines[i + 1]) < 80:
                out["officer_station"] = lines[i + 1]
            break
    accused: List[str] = []
    grab = False
    for ln in lines:
        if re.match(r"^accused\b", ln, re.IGNORECASE):
            grab = True
        elif grab and re.match(r"^date\b", ln, re.IGNORECASE):
            break
        if grab:
            accused.append(ln)
    out["part1_accused"] = "\n".join(accused) or None
    return out


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def transcribe_pages(
    client: VlmClient,
    pngs: List[bytes],
    progress: Optional[Callable[[str], None]] = None,
    trace: Optional[List[dict]] = None,
) -> List[str]:
    """Scanned pages -> markdown per page (the VLM reads text, layout, ticks and bold).

    ``trace`` (evaluation only) receives one record per page: the raw answer and the seconds taken.
    """
    pages: List[str] = []
    for n, png in enumerate(pngs, start=1):
        if progress:
            progress(f"VLM reading page {n} of {len(pngs)}")
        t0 = time.monotonic()
        text = client.transcribe_png(png, PAGE_PROMPT)
        pages.append(text)
        if trace is not None:
            trace.append({"page": n, "seconds": round(time.monotonic() - t0, 2), "raw_response": text})
    return pages


def _map_one(
    client: VlmClient, spec: SectionSpec, chunk: str, trace: Optional[dict] = None
) -> Tuple[Optional[dict], Optional[str]]:
    prompt = build_prompt(spec, chunk[:MAX_CHUNK_CHARS])
    problem = None
    attempts: List[dict] = []
    if trace is not None:
        trace["prompt"] = prompt
        trace["attempts"] = attempts
    for attempt in range(2):
        t0 = time.monotonic()
        try:
            text = client.complete(SYSTEM_PROMPT, prompt if attempt == 0 else prompt + "\nReturn valid JSON only.")
        except VlmError as exc:
            attempts.append({"seconds": round(time.monotonic() - t0, 2), "error": str(exc)})
            return None, str(exc)
        attempts.append({"seconds": round(time.monotonic() - t0, 2), "raw_response": text})
        obj = extract_json(text)
        if obj is not None:
            return obj, None
        problem = "model answer was not valid JSON"
    return None, problem


def map_markdown(
    markdown: str,
    client: VlmClient,
    progress: Optional[Callable[[str], None]] = None,
    evidence_source: str = "document",
    trace: Optional[dict] = None,
) -> dict:
    """Standard IR JSON for a report given as markdown.

    ``evidence_source`` records where the marks (ticks, bold, written Yes) come from:
    ``docx_native`` (read from the Word file) or ``vlm_transcription`` (read from a scan by the VLM).
    ``trace`` (evaluation only) is filled with the raw model output, prompts, timings and the
    evidence found per section; it changes nothing about the result.
    """
    chunks, not_found = split_sections(html_tables_to_markdown(markdown))
    raw: Dict[str, Optional[dict]] = {}
    indexes: Dict[str, EvidenceIndex] = {}
    failures: List[Tuple[str, str]] = []
    if trace is not None:
        trace.update({"sections": {}, "not_found": list(not_found)})

    narrative = chunks.get("s00_narrative", "")
    if narrative:
        raw["s00_narrative"] = parse_narrative(narrative)

    todo = [s for s in SECTIONS if s.number and s.key in chunks]
    for i, spec in enumerate(todo, start=1):
        if progress:
            progress(f"Mapping section {spec.number} ({i} of {len(todo)})")
        index = EvidenceIndex(spec, chunks[spec.key], evidence_source)
        indexes[spec.key] = index
        sec_trace = {} if trace is not None else None
        t0 = time.monotonic()
        obj, problem = _map_one(client, spec, index.restructured, sec_trace)
        if trace is not None:
            sec_trace.update({"seconds": round(time.monotonic() - t0, 2), "source_chunk": chunks[spec.key],
                              "parsed_json": obj, "problem": problem, "evidence": index.dump()})
            trace["sections"][spec.key] = sec_trace
        if obj is None:
            failures.append((spec.key, problem or "unknown error"))
            logger.warning("ir_section_failed section=%s reason=%s", spec.key, problem)
        raw[spec.key] = obj

    result = build_standard(raw, not_found=not_found, indexes=indexes)
    for key, problem in failures:
        result["review_flags"].append(
            {"section": key, "path": key, "code": "mapping_failed", "detail": f"Section could not be mapped: {problem}"}
        )
    if len(chunks.get("s00_narrative", "")) > 0 and not raw.get("s00_narrative", {}).get("part1_facts"):
        result["review_flags"].append(
            {"section": "s00_narrative", "path": "s00_narrative.part1_facts", "code": "missing_required",
             "detail": "Facts of the case (Part-I) were not found"}
        )
    return result
