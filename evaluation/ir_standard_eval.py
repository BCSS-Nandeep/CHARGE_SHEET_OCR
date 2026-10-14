"""Labelled evaluation of the standard-template IR pipeline.

What it does, per report: run the *current* pipeline (no prompt tuning) against the real local VLM,
save everything it produced, and compare the structured JSON with human-verified ground truth.

Rules that keep the numbers honest:

* No accuracy metric is computed unless the ground-truth file says ``"status": "verified"``
  (a human sets that). Without it the report states what is missing; flag rate and timing, which need
  no ground truth, are still reported.
* A draft ground truth can be generated from the document's own marks (Python only, never from the
  model's output) to save labelling effort. It stays ``draft_unverified`` until a person confirms it.
* Fields the ground truth lists under ``ambiguous`` (the source itself is unclear) are left out of the
  accuracy denominators and reported separately.
* Every rate is printed as ``num/den``.

Ground-truth file (JSON)::

    {"report_id": "...", "status": "verified", "verified_by": "...", "verified_at": "2026-10-14",
     "sections": {"s03_physical_features": {"sex": "Male", "ear": "Normal", "language_dialect": "Telugu"},
                  "s05_family_history": {"members": [{"relation": "Father", "name": "..."}]}},
     "ambiguous": {"s03_physical_features.color": ["Brown", "Black"]}}

Only fields present in ``sections`` are scored (the labeller decides the scope; the denominator says how
many). ``null`` means "the report leaves this empty". ``ambiguous`` maps a field to the readings a human
accepts.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from evaluation.text_metrics import character_error_rate, word_error_rate
from app.ir_standard.docx_markdown import docx_to_markdown
from app.ir_standard.evidence import EvidenceIndex
from app.ir_standard.matching import snap_option
from app.ir_standard.sectioner import split_sections
from app.ir_standard.schema import MULTI, SECTION_BY_KEY, SECTIONS, SINGLE, FieldSpec, SectionSpec

VERIFIED, DRAFT = "verified", "draft_unverified"
CATEGORIES = ("ocr_recognition", "field_mapping", "ambiguous_source_mark", "other")


def _dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _norm(text: Any) -> str:
    s = "" if text is None else str(text)
    s = s.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", s).strip(" .,;:-–").casefold()


def _git_head() -> Optional[str]:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip() or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# running the pipeline
# ---------------------------------------------------------------------------

def run_report(entry: dict, out_dir: Path, client, settings) -> dict:
    """Run one report through the current pipeline and save raw output, evidence, JSON and metadata."""
    from app.ir_standard.mapper import map_markdown, transcribe_pages

    rid, kind, path = entry["id"], entry["kind"], Path(entry["path"])
    d = out_dir / rid
    d.mkdir(parents=True, exist_ok=True)
    meta: Dict[str, Any] = {
        "report_id": rid, "kind": kind, "source_file": path.name, "source_bytes": path.stat().st_size,
        "started_at": datetime.now(timezone.utc).isoformat(), "git_head": _git_head(),
        "model": settings.llm_model, "endpoint": settings.llm_base_url, "vlm_dpi": settings.vlm_dpi,
        "host": platform.node(), "warnings": [], "errors": [],
        "note": "Timings include any other load on the machine while the run was in progress.",
    }
    trace: Dict[str, Any] = {}
    page_trace: List[dict] = []
    t_total = time.monotonic()
    try:
        if path.suffix.lower() == ".docx":
            t = time.monotonic()
            markdown = docx_to_markdown(path)
            meta["read_seconds"] = round(time.monotonic() - t, 2)
            pages = [markdown]
            source = "docx_native"
        else:
            import fitz

            t = time.monotonic()
            zoom = settings.vlm_dpi / 72.0
            with fitz.open(path) as doc:
                pngs = [p.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("png") for p in doc]
            meta["render_seconds"] = round(time.monotonic() - t, 2)
            t = time.monotonic()
            pages = transcribe_pages(client, pngs, trace=page_trace)
            meta["transcribe_seconds"] = round(time.monotonic() - t, 2)
            markdown = "\n\n".join(pages)
            source = "vlm_transcription"
        t = time.monotonic()
        standard = map_markdown(markdown, client, evidence_source=source, trace=trace)
        meta["mapping_seconds"] = round(time.monotonic() - t, 2)
    except Exception as exc:  # recorded, never hidden: the run is reported as failed
        meta["errors"].append(f"{type(exc).__name__}: {exc}")
        standard = None
        markdown = ""
        pages = []
    meta["total_seconds"] = round(time.monotonic() - t_total, 2)
    meta["finished_at"] = datetime.now(timezone.utc).isoformat()
    meta["status"] = "failed" if standard is None else "completed"
    if standard is not None:
        meta["warnings"] += [f"{f['code']}: {f['section']}" for f in standard["review_flags"]
                             if f["code"] in ("mapping_failed", "section_not_found")]

    (d / "source_markdown.md").write_text(markdown, encoding="utf-8")
    _dump(d / "raw_vlm_output.json", {
        "pages": page_trace,
        "sections": {k: {"prompt": v.get("prompt"), "attempts": v.get("attempts"), "parsed_json": v.get("parsed_json"),
                         "seconds": v.get("seconds"), "problem": v.get("problem")}
                     for k, v in trace.get("sections", {}).items()},
        "not_found": trace.get("not_found", []),
    })
    _dump(d / "evidence.json", {k: {"source_chunk": v.get("source_chunk"), **(v.get("evidence") or {})}
                                for k, v in trace.get("sections", {}).items()})
    if standard is not None:
        _dump(d / "final_structured.json", standard)
    _dump(d / "run_meta.json", meta)
    return meta


# ---------------------------------------------------------------------------
# ground truth
# ---------------------------------------------------------------------------

def load_ground_truth(path: Optional[Path]) -> Tuple[Optional[dict], str]:
    """(ground truth, reason it cannot be used or "")."""
    if path is None or not Path(path).exists():
        return None, "no ground-truth file"
    gt = json.loads(Path(path).read_text(encoding="utf-8"))
    return gt, ""


def draft_ground_truth(markdown: str, report_id: str) -> dict:
    """Pre-annotation from the document's own marks (Python only; the model is not consulted).

    A choice is drafted only when exactly one option is ticked and nothing else is marked. Anything
    marked only by bold or a written "Yes", or marked in conflicting ways, is left for the labeller
    (listed under ``needs_decision`` with the candidates). Text is drafted where the report prints it
    in a located column / row / label line. Tables and unlocated fields are left out.
    """
    chunks, _ = split_sections(markdown)
    sections: Dict[str, dict] = {}
    basis: Dict[str, str] = {}
    needs: Dict[str, Any] = {}
    for spec in SECTIONS:
        if not spec.number or spec.key not in chunks:
            continue
        index = EvidenceIndex(spec, chunks[spec.key], "docx_native")
        got: Dict[str, Any] = {}
        for f in spec.fields:
            path = f"{spec.key}.{f.key}"
            if f.kind == "text":
                text = index.text_for(f)
                if text is not None:
                    got[f.key] = None if _norm(text) in ("", "nil", "-", "--", "---") and not text.strip() else text
                    basis[path] = "text printed in the field's own column/row"
                continue
            marks = index.marks_for(f)
            if marks is None:
                continue
            state: Dict[str, dict] = {}
            for m in marks:
                o = snap_option(m.label, f.options)
                if o:
                    s = state.setdefault(o, {"ticked": False, "bold": False, "yes": False})
                    s["ticked"] |= m.box == "ticked"
                    s["bold"] |= m.bold
                    s["yes"] |= m.yes
            ticked = [o for o, s in state.items() if s["ticked"]]
            weak = [o for o, s in state.items() if not s["ticked"] and (s["bold"] or s["yes"])]
            if f.kind == SINGLE and len(ticked) == 1 and not weak:
                got[f.key], basis[path] = ticked[0], "single ticked option, nothing else marked"
            elif f.kind == MULTI and ticked and not weak:
                got[f.key], basis[path] = ticked, "ticked options, nothing else marked"
            elif not ticked and not weak and state:
                got[f.key], basis[path] = (None if f.kind == SINGLE else []), "no option marked"
            elif ticked or weak:
                needs[path] = {"ticked": ticked, "bold_or_yes_only": weak}
        if got:
            sections[spec.key] = got
    return {"report_id": report_id, "status": DRAFT, "verified_by": None, "verified_at": None,
            "instructions": "A person must check every value against the report, fill in what is missing, "
                            "decide the entries under needs_decision (move them to sections or ambiguous), "
                            "then set status to 'verified' with verified_by / verified_at.",
            "sections": sections, "ambiguous": {}, "needs_decision": needs, "basis": basis}


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------

def _pred_value(f: FieldSpec, v: Any) -> Any:
    if f.kind == "text":
        return v
    return v.get("selected") if isinstance(v, dict) else None


def _gt_value(f: FieldSpec, raw: Any) -> Any:
    if f.kind == "text":
        return None if raw is None else str(raw)
    if f.kind == SINGLE:
        return None if raw in (None, "") else (snap_option(raw, f.options) or str(raw))
    raws = [] if raw in (None, "") else (raw if isinstance(raw, list) else [raw])
    return sorted({snap_option(r, f.options) or str(r) for r in raws})


def _same(f: FieldSpec, gt: Any, pred: Any) -> bool:
    if f.kind == "text":
        return _norm(gt) == _norm(pred)
    if f.kind == SINGLE:
        return (gt or None) == (pred or None)
    return sorted(gt or []) == sorted(pred or [])


def _empty(f: FieldSpec, value: Any) -> bool:
    return not value if f.kind != "text" else _norm(value) == ""


@dataclass
class Tally:
    num: int = 0
    den: int = 0

    def fmt(self) -> str:
        return f"{self.num}/{self.den}" + (f" = {self.num / self.den:.3f}" if self.den else " (n/a)")

    def to_dict(self) -> dict:
        return {"num": self.num, "den": self.den, "value": round(self.num / self.den, 4) if self.den else None}


def _evidence_summary(ev_field: Optional[dict], std_field: Any) -> dict:
    out: Dict[str, Any] = {}
    if ev_field:
        out["marks"] = [{"option_text": m["label"], "box": m["box"], "bold": m["bold"], "yes": m["yes"],
                         "state": m["state"], "at": m["at"]} for m in (ev_field.get("marks") or [])]
        out["column_or_row_text"] = ev_field.get("text")
    if isinstance(std_field, dict) and std_field.get("verification"):
        v = std_field["verification"]
        out["verification_status"] = v["status"]
        out["chosen"] = v["chosen"]
        out["candidates"] = v["candidates"]
    return out


def _categorize(f: FieldSpec, gt: Any, pred: Any, kind: str, chunk: str, ev: dict, ambiguous: bool) -> Tuple[str, str]:
    """(category, why) for a wrong / missing / spurious value. Rules are heuristic and documented in the report."""
    if ambiguous:
        return "ambiguous_source_mark", "the ground truth lists this field as ambiguous in the source"
    status = ev.get("verification_status")
    cands = {c["option"] for c in ev.get("candidates", [])}
    if f.kind != "text":
        gt_opts = {gt} if f.kind == SINGLE and gt else set(gt or [])
        if status in ("conflicting", "uncertain") and gt_opts & cands:
            return "ambiguous_source_mark", "the report marks the ground-truth option only weakly or conflictingly"
        marked = {m["option_text"] for m in ev.get("marks", []) if m["state"] != "unselected"}
        marked = {snap_option(m, f.options) for m in marked}
        if gt_opts and not (gt_opts & marked):
            if kind.startswith("scan"):
                return "ocr_recognition", "the ground-truth option is not marked in the VLM's transcription"
            return "other", "the report's own marks do not show the ground-truth option"
        return "field_mapping", "the report marks the ground-truth option but the model returned something else"
    in_source = _norm(gt) in _norm(chunk) if gt else False
    if gt and pred:
        cer = character_error_rate(_norm(gt), _norm(pred))
        if cer.value is not None and cer.value <= 0.35:
            return ("ocr_recognition", "close to the ground truth: character-level misreading") if kind.startswith("scan") \
                else ("other", "close to the ground truth: copy / normalisation difference")
    if gt and not in_source:
        return ("ocr_recognition", "the ground-truth text is not in the transcription") if kind.startswith("scan") \
            else ("other", "the ground-truth text is not in the report text for this section")
    if pred and not _norm(pred) in _norm(chunk):
        return "other", "the returned text does not appear in the section's source text"
    return "field_mapping", "the value is in the section's text but the model returned a different or empty value"


def compare(run_dir: Path, gt: Optional[dict], gt_problem: str, kind: str, allow_draft: bool = False) -> dict:
    """Metrics + field-level errors for one saved run."""
    std = json.loads((run_dir / "final_structured.json").read_text(encoding="utf-8")) if (run_dir / "final_structured.json").exists() else None
    meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    evidence = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8")) if (run_dir / "evidence.json").exists() else {}
    result: Dict[str, Any] = {"report_id": meta["report_id"], "kind": kind, "run_status": meta["status"],
                              "time": {k: meta.get(k) for k in ("total_seconds", "read_seconds", "render_seconds",
                                                               "transcribe_seconds", "mapping_seconds")},
                              "errors": meta["errors"], "warnings": meta["warnings"]}
    if std is None:
        result["status"] = "NOT_MEASURED"
        result["reason"] = "the pipeline run failed: " + "; ".join(meta["errors"])
        return result

    flags = std["review_flags"]
    section_flagged = {f["section"] for f in flags if f["code"] in ("mapping_failed", "section_not_found")}
    by_path: Dict[str, List[dict]] = {}
    for f in flags:
        by_path.setdefault(f["path"], []).append(f)

    # ---- metrics that need no ground truth: flag rate -------------------------------------------
    all_fields: List[Tuple[SectionSpec, FieldSpec]] = [
        (s, f) for s in SECTIONS if s.number and s.key not in {x for x in section_flagged if x} for f in s.fields]
    flagged_all = [(s, f) for s, f in all_fields if by_path.get(f"{s.key}.{f.key}")]
    codes: Dict[str, int] = {}
    for fl in flags:
        codes[fl["code"]] = codes.get(fl["code"], 0) + 1
    result["review_flags"] = {
        "template_fields_in_found_sections": len(all_fields), "fields_flagged": len(flagged_all),
        "flag_rate": Tally(len(flagged_all), len(all_fields)).to_dict(), "total_flags": len(flags), "by_code": codes,
        "sections_not_found": sorted(f["section"] for f in flags if f["code"] == "section_not_found"),
        "sections_failed": sorted(f["section"] for f in flags if f["code"] == "mapping_failed"),
    }
    result["flagged_fields"] = [
        {"path": f"{s.key}.{f.key}", "label": f.label, "predicted": _pred_value(f, std["sections"][s.key][f.key]),
         "flags": [{"code": x["code"], "detail": x["detail"]} for x in by_path[f"{s.key}.{f.key}"]],
         "evidence": _evidence_summary(evidence.get(s.key, {}).get("fields", {}).get(f.key), std["sections"][s.key][f.key])}
        for s, f in flagged_all]

    # ---- ground-truth gate -------------------------------------------------------------------
    if gt is None:
        result.update(status="NOT_MEASURED", reason=f"accuracy metrics not computed: {gt_problem}")
        return result
    gstat = gt.get("status")
    if gstat != VERIFIED and not (allow_draft and gstat == DRAFT):
        result.update(status="NOT_MEASURED", reason=f"accuracy metrics not computed: ground truth status is {gstat!r}, "
                      f"a person must verify it and set status to 'verified'")
        return result
    result["status"] = "MEASURED" if gstat == VERIFIED else "UNVERIFIED_DRAFT_AGREEMENT_NOT_ACCURACY"
    result["ground_truth"] = {"status": gstat, "verified_by": gt.get("verified_by"), "verified_at": gt.get("verified_at")}

    ambiguous = gt.get("ambiguous") or {}
    acc_all, acc_by_kind = Tally(), {"text": Tally(), "single": Tally(), "multi": Tally()}
    cer_err = cer_ref = wer_err = wer_ref = 0
    tp = fp = fn = 0  # checkbox (field, option) pairs
    val = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    amb_total = amb_match = 0
    unlabelled = 0
    rows: List[dict] = []
    for s, f in [(s, f) for s in SECTIONS if s.number for f in s.fields]:
        path = f"{s.key}.{f.key}"
        sec_gt = (gt.get("sections") or {}).get(s.key, {})
        in_gt = f.key in sec_gt
        if not in_gt and path not in ambiguous:
            unlabelled += 1
            continue
        pred_raw = std["sections"][s.key][f.key]
        pred = _pred_value(f, pred_raw)
        if f.kind == MULTI:
            pred = sorted(pred or [])
        fl = by_path.get(path, [])
        flagged = bool(fl) or s.key in section_flagged
        ev = _evidence_summary(evidence.get(s.key, {}).get("fields", {}).get(f.key), pred_raw)
        chunk = evidence.get(s.key, {}).get("source_chunk") or ""
        if path in ambiguous:
            acceptable = ambiguous[path]
            ok = any(_same(f, _gt_value(f, a), pred) for a in acceptable)
            amb_total += 1
            amb_match += ok
            rows.append({"path": path, "label": f.label, "kind": f.kind, "ground_truth": acceptable, "predicted": pred,
                         "correct": ok, "ambiguous": True, "flags": [x["code"] for x in fl], "evidence": ev,
                         "error_category": "ambiguous_source_mark", "why": "listed as ambiguous by the labeller",
                         "validation_outcome": "n/a (ambiguous, excluded from validation precision/recall)",
                         "human_review_required": flagged})
            continue
        gtv = _gt_value(f, sec_gt[f.key])
        correct = _same(f, gtv, pred)
        acc_all.den += 1
        acc_all.num += correct
        acc_by_kind[f.kind].den += 1
        acc_by_kind[f.kind].num += correct
        if f.kind == "text" and not _empty(f, gtv):
            c = character_error_rate(_norm(gtv), _norm(pred))
            w = word_error_rate(_norm(gtv), _norm(pred))
            cer_err, cer_ref, wer_err, wer_ref = cer_err + c.errors, cer_ref + c.reference_units, wer_err + w.errors, wer_ref + w.reference_units
        if f.kind != "text":
            g = set([gtv] if (f.kind == SINGLE and gtv) else (gtv or []))
            p = set([pred] if (f.kind == SINGLE and pred) else (pred or []))
            tp, fp, fn = tp + len(g & p), fp + len(p - g), fn + len(g - p)
        outcome = ("tp" if flagged else "fn") if not correct else ("fp" if flagged else "tn")
        val[outcome] += 1
        if correct and not flagged:
            continue
        if correct:
            cat, why = "none", "value matches the ground truth but was flagged for review"
        else:
            cat, why = _categorize(f, gtv, pred, kind, chunk, ev, False)
        rows.append({"path": path, "section": s.key, "label": f.label, "kind": f.kind, "ground_truth": gtv, "predicted": pred,
                     "correct": correct, "flags": [{"code": x["code"], "detail": x["detail"]} for x in fl]
                     + ([{"code": "section_level", "detail": "section not found / could not be mapped"}] if s.key in section_flagged else []),
                     "evidence": ev, "error_category": cat, "why": why, "validation_outcome": outcome,
                     "human_review_required": flagged or not correct})

    # ---- tables ---------------------------------------------------------------------------------
    cells = Tally()
    table_rows: List[dict] = []
    for s in SECTIONS:
        for t in s.tables:
            gt_rows = ((gt.get("sections") or {}).get(s.key) or {}).get(t.key)
            if gt_rows is None:
                continue
            pred_rows = std["sections"][s.key][t.key]
            for i, grow in enumerate(gt_rows):
                if t.fixed_rows:
                    label = grow.get(t.columns[0].key)
                    prow = next((r for r in pred_rows if _norm(r.get(t.columns[0].key)) == _norm(label)), None)
                else:
                    prow = pred_rows[i] if i < len(pred_rows) else None
                for c in t.columns:
                    if c.key not in grow or (t.fixed_rows and c is t.columns[0]):
                        continue
                    gv = _gt_value(c, grow[c.key])
                    pv = _pred_value(c, prow.get(c.key)) if prow else None
                    pv = sorted(pv or []) if c.kind == MULTI else pv
                    ok = _same(c, gv, pv)
                    cells.den += 1
                    cells.num += ok
                    if not ok:
                        table_rows.append({"path": f"{s.key}.{t.key}.{i}.{c.key}", "label": f"{t.title} / row {i + 1} / {c.label}",
                                           "ground_truth": gv, "predicted": pv, "row_found": prow is not None})
    result["table_cell_accuracy"] = {"cells_correct": cells.to_dict(),
                                     "note": "rows are aligned by position (fixed-row tables by relation); extra predicted rows are not scored"}
    result["table_errors"] = table_rows

    # ---- aggregate -----------------------------------------------------------------------------
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    vp = val["tp"] / (val["tp"] + val["fp"]) if val["tp"] + val["fp"] else None
    vr = val["tp"] / (val["tp"] + val["fn"]) if val["tp"] + val["fn"] else None
    result["metrics"] = {
        "denominator_note": "scored fields = fields listed in the ground truth, excluding ones the labeller marked ambiguous; "
                            f"{unlabelled} template field(s) were not labelled and are not scored",
        "field_accuracy": acc_all.to_dict(), "field_accuracy_by_kind": {k: v.to_dict() for k, v in acc_by_kind.items()},
        "ambiguous_fields": {"listed": amb_total, "model_matches_an_accepted_reading": amb_match},
        "checkbox": {"definition": "pairs of (field, option): true positive = selected in both; precision = TP/(TP+FP); "
                                   "recall = TP/(TP+FN); unselected options are not counted",
                     "tp": tp, "fp": fp, "fn": fn,
                     "precision": None if prec is None else round(prec, 4), "recall": None if rec is None else round(rec, 4)},
        "text_error_rates": {"scope": "text fields whose ground truth is non-empty, case/space/edge-punctuation normalised; "
                                      "a missing prediction counts as full deletion",
                             "cer": {"errors": cer_err, "reference_chars": cer_ref, "value": round(cer_err / cer_ref, 4) if cer_ref else None},
                             "wer": {"errors": wer_err, "reference_words": wer_ref, "value": round(wer_err / wer_ref, 4) if wer_ref else None}},
        "validation": {"definition": "flagged = field has a review flag; positive = value differs from ground truth",
                       **val, "precision": None if vp is None else round(vp, 4), "recall": None if vr is None else round(vr, 4)},
        "bold_detection": "not measured (the ground truth carries no formatting labels)",
    }
    cat_counts = {c: 0 for c in CATEGORIES}
    for r in rows:
        if r["error_category"] in cat_counts and (not r["correct"] or r.get("ambiguous")):
            cat_counts[r["error_category"]] += 1
    result["error_categories"] = {**cat_counts,
                                  "validation_false_positive": val["fp"], "validation_false_negative": val["fn"]}
    result["field_errors"] = [r for r in rows]
    return result


# ---------------------------------------------------------------------------
# human-readable report
# ---------------------------------------------------------------------------

def _cell(v: Any, n: int = 60) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    s = s.replace("|", "/").replace("\n", " ")
    return (s[: n - 1] + "…") if len(s) > n else s


def to_markdown(r: dict) -> str:
    L = [f"# {r['report_id']}  ({r['kind']})", ""]
    if r["status"] != "MEASURED":
        L += [f"> **{r['status']}**" + (f": {r['reason']}" if r.get("reason") else ""), ""]
    t = r["time"]
    L += ["## Run", f"- status: {r['run_status']}; total {t.get('total_seconds')} s"
          + "".join(f"; {k.replace('_seconds', '')} {v} s" for k, v in t.items() if k != "total_seconds" and v is not None),
          f"- errors: {r['errors'] or 'none'}; warnings: {r['warnings'] or 'none'}", ""]
    rf = r.get("review_flags")
    if rf:
        L += ["## Review flags (no ground truth needed)",
              f"- fields flagged: {rf['fields_flagged']}/{rf['template_fields_in_found_sections']} "
              f"= {rf['flag_rate']['value']} (denominator: template fields in sections the report contained)",
              f"- total flags {rf['total_flags']}: {rf['by_code']}",
              f"- sections not found: {rf['sections_not_found'] or 'none'}; sections that failed: {rf['sections_failed'] or 'none'}", ""]
    m = r.get("metrics")
    if m:
        c, tx, v = m["checkbox"], m["text_error_rates"], m["validation"]
        L += ["## Metrics", f"- {m['denominator_note']}",
              f"- field accuracy: {m['field_accuracy']['num']}/{m['field_accuracy']['den']} = {m['field_accuracy']['value']}"
              f"  (by kind: " + ", ".join(f"{k} {x['num']}/{x['den']}" for k, x in m['field_accuracy_by_kind'].items()) + ")",
              f"- ambiguous fields excluded: {m['ambiguous_fields']}",
              f"- checkbox precision {c['precision']} / recall {c['recall']} (TP {c['tp']}, FP {c['fp']}, FN {c['fn']}); {c['definition']}",
              f"- CER {tx['cer']['errors']}/{tx['cer']['reference_chars']} = {tx['cer']['value']}; "
              f"WER {tx['wer']['errors']}/{tx['wer']['reference_words']} = {tx['wer']['value']}; {tx['scope']}",
              f"- validation precision {v['precision']} / recall {v['recall']} (TP {v['tp']}, FP {v['fp']}, FN {v['fn']}, TN {v['tn']})",
              f"- table cells correct: {r['table_cell_accuracy']['cells_correct']['num']}/{r['table_cell_accuracy']['cells_correct']['den']}",
              f"- bold detection: {m['bold_detection']}", f"- error categories: {r['error_categories']}", ""]
        L += ["## Field-level errors and flagged-but-correct fields",
              "| field | gt | predicted | correct | category | validation | flags | review |", "|---|---|---|---|---|---|---|---|"]
        for e in r["field_errors"]:
            L.append(f"| {e['path'].split('.', 1)[1] if '.' in e['path'] else e['path']} | {_cell(e['ground_truth'])} | {_cell(e['predicted'])} | "
                     f"{e['correct']} | {e['error_category']} | {e['validation_outcome']} | "
                     f"{_cell([x['code'] if isinstance(x, dict) else x for x in e['flags']], 50)} | {'yes' if e['human_review_required'] else 'no'} |")
        L.append("")
        if r["table_errors"]:
            L += ["## Table cell errors", "| cell | gt | predicted |", "|---|---|---|"]
            L += [f"| {_cell(e['label'], 70)} | {_cell(e['ground_truth'])} | {_cell(e['predicted'])} |" for e in r["table_errors"]]
            L.append("")
    if r.get("flagged_fields"):
        L += ["## Flagged fields (value shown as returned; nothing was replaced)",
              "| field | predicted | flags | evidence |", "|---|---|---|---|"]
        for f in r["flagged_fields"]:
            ev = f["evidence"]
            marks = "; ".join(f"{m['option_text']}[{m['box']}{',bold' if m['bold'] else ''}{',yes' if m['yes'] else ''}]"
                              for m in ev.get("marks", []) if m["state"] != "unselected") or ev.get("column_or_row_text") or ""
            L.append(f"| {f['path'].split('.', 1)[1]} | {_cell(f['predicted'], 40)} | {_cell([x['code'] for x in f['flags']], 60)} | {_cell(marks, 90)} |")
    return "\n".join(L) + "\n"


def write_comparison(run_dir: Path, result: dict, gt: Optional[dict]) -> None:
    if gt is not None:
        _dump(run_dir / "ground_truth.json", gt)
    _dump(run_dir / "comparison.json", result)
    (run_dir / "field_errors.md").write_text(to_markdown(result), encoding="utf-8")
    _dump(run_dir / "field_errors.json", {"field_errors": result.get("field_errors", []),
                                          "table_errors": result.get("table_errors", []),
                                          "flagged_fields": result.get("flagged_fields", [])})
