"""Same-template / different-data variants for robustness testing.

A *variant spec* (JSON, kept in the private fixtures because it names real
strings) describes how to turn one real document into many documents of the
same template with different data:

  variables   name → type (given, surname, place, district, age, phone, date_in:<var>, choice:a|b, ...)
  regex       [[pattern, replacement]]  — contextual values (ages, dates, sections)
  names       {old phrase: new template} — names/places; matched with flexible whitespace
              (so glued "PartigallaNikilkumar" and spaced forms both match), case-shape preserving
  gt          {json path: template}      — ground-truth fields not covered by text rules
  witness_rows / ocr_witness_bands / accused_drop — structural edits (add / drop entries)

Substitution happens on the *extracted* representation (PDF text, positioned
lines, table cells, OCR boxes), so every layout quirk of the real template is
preserved while the data changes. OCR recognition of the new strings is not
exercised by this (the original OCR boxes are reused).
"""

from __future__ import annotations

import copy
import json
import random
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

GIVEN = [
    "Raghava", "Srinu", "Venkanna", "Mallesham", "Bhaskar", "Chandraiah", "Devender", "Eshwar", "Gangaram",
    "Hanmanthu", "Ilaiah", "Jangaiah", "Narender", "Omkar", "Prabhakar", "Rajaiah", "Sammaiah", "Upender",
    "Veeresham", "Yellaiah", "Anjaneyulu", "Balraj", "Harish", "Jagadish", "Madhusudhan", "Nagesh",
    "Ramchander", "Sathaiah", "Shivaraj", "Somaiah", "Vinod", "Yadaiah", "Kanakaiah", "Lingaiah", "Pochaiah",
]
SURNAME = [
    "Kothapalli", "Bandari", "Gandla", "Pothula", "Chakali", "Boya", "Mekala", "Thatikonda",
    "Nalla", "Bairi", "Dasari", "Vadde", "Sangem", "Pittala", "Jangili", "Mudavath", "Bhukya", "Guguloth",
    "Ediga", "Kandula", "Mamidi", "Pasula", "Rapolu", "Siliveri", "Tallapalli", "Ummadi", "Vemula",
]
PLACE = [
    "Ramayampet", "Kowdipally", "Narsapur", "Chegunta", "Tekmal", "Nizampet", "Regode", "Yeldurthy",
    "Alladurg", "Kulcharam", "Chilipched", "Nagaram", "Pulkal", "Andole", "Jogipet", "Kohir", "Raikode",
    "Munipally", "Kondapur", "Venkatapur",
]
PS_PREFIX = ["Chinna", "Pedda", "Kotha", "Mallam", "Rama"]
DISTRICT = ["Siddipet", "Nizamabad", "Vikarabad", "Jangaon", "Nalgonda", "Khammam", "Suryapet", "Mancherial", "Jagtial", "Nirmal"]
OCCUPATION = ["Driver", "Mason", "Tailor", "Mechanic", "Coolie", "Carpenter", "Electrician", "Barber", "Weaver"]
SUFFIX = ["Goud", "Yadav", "Naik", "Chary", "Rao"]

_POOLS = {"given": GIVEN, "surname": SURNAME, "place": PLACE, "district": DISTRICT,
          "occupation": OCCUPATION, "suffix": SUFFIX, "ps_prefix": PS_PREFIX}


def _date_str(d: date, sep: str = ".") -> str:
    return f"{d.day:02d}{sep}{d.month:02d}{sep}{d.year}"


def _parse(s: str) -> date:
    d, m, y = re.split(r"[./\-]", s)
    return date(int(y), int(m), int(d))


def _swap(v: str) -> str:
    mid = len(v) // 2
    return v[:mid] + ("x" if v[mid].lower() != "x" else "k") + v[mid + 1 :]


# Spelling / format modifiers usable in templates: {name__dropinner__lower}
MODIFIERS = {
    "upper": str.upper,
    "lower": str.lower,
    "glued": lambda v: v.replace(" ", ""),
    "dropfirst": lambda v: v[1:2].upper() + v[2:],  # "Aerrolla" → "Errolla"
    "dropinner": lambda v: v[: len(v) // 2] + v[len(v) // 2 + 1 :],  # "Kishta" → "Kista"
    "droplast": lambda v: v[:-1],  # "Kodasu" → "Kodas"
    "swap": _swap,  # one inner letter replaced
    "minus1": lambda v: str(int(v) - 1),
    "dash": lambda v: v.replace(".", "-"),
    "slash": lambda v: v.replace(".", "/"),
}


def generate_variables(spec: dict, seed: int, exclude: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    rng = random.Random(seed)
    taken = set((exclude or {}).values())
    used: Dict[str, set] = {k: {v for v in pool if v in taken} for k, pool in _POOLS.items()}
    values: Dict[str, str] = {}
    for name, typ in spec.get("variables", {}).items():
        kind, _, arg = typ.partition(":")
        if kind in _POOLS:
            choices = [v for v in _POOLS[kind] if v not in used[kind]] or _POOLS[kind]
            v = rng.choice(choices)
            used[kind].add(v)
        elif kind == "age":
            v = str(rng.randint(19, 65))
        elif kind == "phone":
            v = str(rng.randint(6, 9)) + "".join(str(rng.randint(0, 9)) for _ in range(9))
        elif kind == "aadhaar":
            v = str(rng.randint(2, 9)) + "".join(str(rng.randint(0, 9)) for _ in range(11))
        elif kind == "year":
            v = str(rng.randint(2016, 2025))
        elif kind == "num":
            lo, hi = (int(x) for x in arg.split("-"))
            v = str(rng.randint(lo, hi))
        elif kind == "date_in":
            year = int(values[arg])
            v = _date_str(date(year, 1, 1) + timedelta(days=rng.randint(0, 300)))
        elif kind == "date_after":
            base = _parse(values[arg])
            v = _date_str(base + timedelta(days=rng.randint(20, 330)))
        elif kind == "choice":
            v = rng.choice(arg.split("|"))
        elif kind == "const":
            v = arg
        else:
            raise ValueError(f"unknown variable type {typ!r}")
        values[name] = v
    return values


# ---------------------------------------------------------------------------
# Text substitution
# ---------------------------------------------------------------------------


def _resolve(key: str, variables: Dict[str, str]) -> str:
    name, *mods = key.split("__")
    value = variables[name]
    for mod in mods:
        value = MODIFIERS[mod](value)
    return value


def _render(template: str, variables: Dict[str, str]) -> str:
    return re.sub(r"\{([A-Za-z0-9_]+)\}", lambda m: _resolve(m.group(1), variables), template)


def _shape(new: str, matched: str) -> str:
    letters = [c for c in matched if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and len(letters) > 1:
        new = new.upper()
    elif matched[:1].islower():
        new = new[:1].lower() + new[1:]
    if not re.search(r"\s", matched) and " " in new:
        new = new.replace(" ", "")
    return new


def _left_ok(text: str, start: int) -> bool:
    if start == 0 or not text[start - 1].isalpha():
        return True
    if text[start].isupper() and text[start - 1].islower():
        return True  # camelCase glue
    return text[max(0, start - 2) : start].lower() in ("/o", "/.")  # "S/osailu"


def _right_ok(text: str, end: int) -> bool:
    if end >= len(text) or not text[end].isalpha():
        return True
    return text[end].isupper() and text[end - 1].islower()


@dataclass
class Rules:
    regex: List[Tuple[re.Pattern, str]]
    names: List[Tuple[re.Pattern, str]]

    def apply(self, text: str) -> str:
        """Single pass over the original text, so a new value is never re-matched by another rule."""
        if not text:
            return text
        matches = []  # (start, priority, -length, rule order, end, replacement)
        for order, (rx, repl) in enumerate(self.regex):
            for m in rx.finditer(text):
                if m.end() > m.start():
                    matches.append((m.start(), 0, -(m.end() - m.start()), order, m.end(), m.expand(repl)))
        for order, (rx, new) in enumerate(self.names):
            for m in rx.finditer(text):
                if _left_ok(text, m.start()) and _right_ok(text, m.end()):
                    # A name broken across lines keeps its line break after the new value,
                    # so line-structured text keeps the same number of lines.
                    breaks = "\n" * m.group(0).count("\n")
                    matches.append((m.start(), 1, -(m.end() - m.start()), order, m.end(), _shape(new, m.group(0)) + breaks))
        matches.sort(key=lambda t: t[:4])
        out, pos = [], 0
        for start, _, _, _, end, replacement in matches:
            if start < pos:
                continue
            out.append(text[pos:start])
            out.append(replacement)
            pos = end
        out.append(text[pos:])
        return "".join(out)


def apply_to_lines(rules: Rules, lines: List[str]) -> List[str]:
    """Substitute across line boundaries (names split over two lines), keeping the line count."""
    joined = rules.apply("\n".join(lines))
    out = joined.split("\n")
    if len(out) == len(lines):
        return out
    return [rules.apply(line) for line in lines]


def compile_rules(section: dict, variables: Dict[str, str]) -> Rules:
    regex = [
        (re.compile(pat, re.I | re.M), _render(repl, variables).replace("\\\\", "\\"))
        for pat, repl in section.get("regex", [])
    ]
    names = []
    for old, new in sorted(section.get("names", {}).items(), key=lambda kv: -len(kv[0])):
        pattern = r"\s*".join(re.escape(tok) for tok in old.split())
        names.append((re.compile(pattern, re.I), _render(new, variables)))
    return Rules(regex, names)


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------

_GT_SKIP_KEYS = {"field", "note", "document_id", "description", "layout", "source", "verification",
                 "known_gaps", "known_gaps_reason", "requires_ocr"}


def _map_strings(obj: Any, fn, key: Optional[str] = None) -> Any:
    if key in _GT_SKIP_KEYS:
        return obj
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_map_strings(v, fn) for v in obj]
    if isinstance(obj, dict):
        return {k: _map_strings(v, fn, k) for k, v in obj.items()}
    return obj


def _set_path(obj: Any, path: str, value: Any) -> None:
    parts = re.findall(r"[^.\[\]]+|\[\d+\]", path)
    cur = obj
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        key: Any = int(part[1:-1]) if part.startswith("[") else part
        if last:
            cur[key] = value
        else:
            cur = cur[key]


def _remap_conflicts(conflicts: List[dict], kind: str, mapping: Dict[int, Optional[int]]) -> List[dict]:
    out = []
    for c in conflicts:
        m = re.match(rf"^{kind}\[(\d+)\](.*)$", c["field"])
        if not m:
            out.append(c)
            continue
        new = mapping.get(int(m.group(1)), int(m.group(1)))
        if new is None:
            continue
        out.append({**c, "field": f"{kind}[{new}]{m.group(2)}"})
    return out


# ---------------------------------------------------------------------------
# Variant construction
# ---------------------------------------------------------------------------


@dataclass
class Variant:
    seed: int
    variables: Dict[str, str]
    ground_truth: dict
    pdf_result: Any = None  # transformed PdfExtractionResult (native templates)
    ocr_pages: Optional[List[dict]] = None  # transformed OCR boxes (scanned templates)
    description: str = ""


def _renumber(cells: List[str], number: int) -> List[str]:
    for i, c in enumerate(cells):
        if c.strip():
            width = len(c.strip()) if c.strip().isdigit() else 1
            cells[i] = str(number).zfill(width)
            break
    return cells


def _apply_table_rows(pdf_result, spec: dict, rules: Rules, variables: Dict[str, str], rng: random.Random,
                      gt: dict, notes: List[str], protected: set) -> None:
    ws = spec.get("witness_rows")
    if not ws:
        return
    pages = {p.page_number: p for p in pdf_result.pages}
    refs = [(pages[pg].tables[t], r) for pg, t, r in ws["refs"]]
    rows = [(table, table.rows[r]) for table, r in refs]
    witnesses = gt["expected"]["witnesses"]
    index_map: Dict[int, Optional[int]] = {i: i for i in range(len(witnesses))}
    entries = list(range(len(rows)))  # original indices in order

    op = rng.choice(ws.get("ops", ["none"]))
    if op == "drop":
        k = rng.randint(1, min(ws.get("max_drop", 2), len(rows) - 1))
        dropped = sorted(rng.sample([i for i in range(len(rows)) if i not in ws.get("keep", [])], k))
        for i in dropped:
            table, row = rows[i]
            idx = next(j for j, r in enumerate(table.rows) if r is row)
            del table.rows[idx]
            if idx < len(table.row_tops):
                del table.row_tops[idx]
        entries = [i for i in entries if i not in dropped]
        notes.append(f"dropped witness rows {[i + 1 for i in dropped]}")
    elif op == "add":
        tpl = ws["add_template"]
        n_add = rng.randint(1, tpl.get("max", 2))
        src_index = tpl["source_index"]
        table, src_row = rows[src_index]
        insert_at = next(j for j, r in enumerate(table.rows) if r is src_row) + 1
        new_entries = []
        for n in range(n_add):
            extra_vars = generate_variables({"variables": tpl["variables"]}, seed=rng.randint(0, 10**9), exclude=variables)
            local = compile_rules(tpl, {**variables, **extra_vars})
            # One pass: the entry's own values first, then global names (places etc.).
            # Global value rules (ages, phones) are not applied — a new random age could
            # equal an original value that a global rule rewrites.
            combined = Rules(local.regex, local.names + rules.names)
            new_row = [combined.apply(c) for c in src_row]
            protected.add(id(new_row))
            table.rows.insert(insert_at + n, new_row)
            top = table.row_tops[insert_at - 1] + 0.1 * (n + 1) if table.row_tops else 0.0
            table.row_tops.insert(insert_at + n, top)
            gt_entry = {k: _render(v, {**variables, **extra_vars}) for k, v in tpl["gt"].items()}
            new_entries.append(("new", gt_entry, new_row))
        pos = entries.index(src_index) + 1
        entries = entries[:pos] + new_entries + entries[pos:]
        notes.append(f"added {n_add} witness row(s) after #{src_index + 1}")

    # Renumber all witness rows in reading order and rebuild GT
    new_witnesses = []
    for new_i, e in enumerate(entries):
        if isinstance(e, tuple):
            _, gt_entry, row = e
            _renumber(row, new_i + 1)
            new_witnesses.append({**gt_entry, "serial_no": str(new_i + 1)})
        else:
            _renumber(rows[e][1], new_i + 1)
            new_witnesses.append({**witnesses[e], "serial_no": str(new_i + 1)})
    for old_i in index_map:
        index_map[old_i] = next((ni for ni, e in enumerate(entries) if e == old_i), None)
    gt["expected"]["witnesses"] = new_witnesses
    gt["expected"]["conflicts"] = _remap_conflicts(gt["expected"]["conflicts"], "witness", index_map)


def _apply_accused_drop(pdf_result, spec: dict, rng: random.Random, gt: dict, notes: List[str]) -> None:
    ad = spec.get("accused_drop")
    if not ad or rng.random() >= ad.get("probability", 0.5):
        return
    pages = {p.page_number: p for p in pdf_result.pages}
    pg, t, r, col = ad["cell"]
    row = pages[pg].tables[t].rows[r]
    row[col] = re.sub(ad["regex"], "", row[col], flags=re.S)
    _drop_accused_gt(gt, ad["gt_index"])
    notes.append(f"dropped accused #{ad['gt_index'] + 1}")


def _drop_accused_gt(gt: dict, index: int) -> None:
    acc = gt["expected"]["accused"]
    del acc[index]
    mapping = {i: (None if i == index else (i - 1 if i > index else i)) for i in range(len(acc) + 1)}
    gt["expected"]["conflicts"] = _remap_conflicts(gt["expected"]["conflicts"], "accused", mapping)


def _apply_ocr_bands(pages: List[dict], spec: dict, rules: Rules, variables: Dict[str, str], rng: random.Random,
                     gt: dict, notes: List[str]) -> None:
    wb = spec.get("ocr_witness_bands")
    if not wb:
        return
    page = next(p for p in pages if p["page"] == wb["page"])
    lines = page["lines"]
    anchors = []
    for serial in wb["serials"]:
        a = next(l for l in lines if l["text"].strip() == serial and l["box"][0] < wb["anchor_max_x"]
                 and l["box"][1] > wb["y_min"])
        anchors.append(a)
    tops = [a["box"][1] - 8 for a in anchors]
    height = sorted(t2 - t1 for t1, t2 in zip(tops, tops[1:]))[len(tops) // 2]
    ends = tops[1:] + [tops[-1] + height]

    def band(i):
        return [l for l in lines if tops[i] <= (l["box"][1] + l["box"][3]) / 2 < ends[i]]

    witnesses = gt["expected"]["witnesses"]
    entries: List[Any] = list(range(len(anchors)))
    op = rng.choice(wb.get("ops", ["none"]))
    if op == "drop":
        i = rng.choice([j for j in range(len(anchors)) if j not in wb.get("keep", [])])
        removed = band(i)
        span = ends[i] - tops[i]
        for l in removed:
            lines.remove(l)
        for l in lines:
            if (l["box"][1] + l["box"][3]) / 2 >= ends[i]:
                l["box"] = [l["box"][0], l["box"][1] - span, l["box"][2], l["box"][3] - span]
        entries.remove(i)
        notes.append(f"dropped witness band #{i + 1}")
    elif op == "add":
        tpl = wb["add_template"]
        i = tpl["source_index"]
        extra_vars = generate_variables({"variables": tpl["variables"]}, seed=rng.randint(0, 10**9), exclude=variables)
        local = compile_rules(tpl, {**variables, **extra_vars})
        span = ends[i] - tops[i]
        src = band(i)
        for l in lines:
            if (l["box"][1] + l["box"][3]) / 2 >= ends[i]:
                l["box"] = [l["box"][0], l["box"][1] + span, l["box"][2], l["box"][3] + span]
        combined = Rules(local.regex, local.names + rules.names)  # see _apply_table_rows
        for l in src:
            nl = copy.deepcopy(l)
            nl["box"] = [l["box"][0], l["box"][1] + span, l["box"][2], l["box"][3] + span]
            nl["text"] = combined.apply(nl["text"])
            nl["_protected"] = True
            lines.append(nl)
        gt_entry = {k: _render(v, {**variables, **extra_vars}) for k, v in tpl["gt"].items()}
        entries.insert(entries.index(i) + 1, ("new", gt_entry))
        notes.append(f"added a witness band after #{i + 1}")

    # Renumber serial anchors top to bottom
    serial_boxes = sorted(
        [l for l in lines if re.fullmatch(r"\d{1,2}", l["text"].strip()) and l["box"][0] < wb["anchor_max_x"]
         and l["box"][1] > wb["y_min"]],
        key=lambda l: l["box"][1],
    )
    for n, l in enumerate(serial_boxes):
        l["text"] = str(n + 1)
    new_witnesses = []
    for n, e in enumerate(entries):
        base = e[1] if isinstance(e, tuple) else witnesses[e]
        new_witnesses.append({**base, "serial_no": str(n + 1)})
    mapping = {old: next((n for n, e in enumerate(entries) if e == old), None) for old in range(len(witnesses))}
    gt["expected"]["witnesses"] = new_witnesses
    gt["expected"]["conflicts"] = _remap_conflicts(gt["expected"]["conflicts"], "witness", mapping)


def _apply_ocr_accused_drop(pages: List[dict], spec: dict, rng: random.Random, gt: dict, notes: List[str]) -> None:
    ad = spec.get("ocr_accused_drop")
    if not ad or rng.random() >= ad.get("probability", 0.5):
        return
    page = next(p for p in pages if p["page"] == ad["page"])
    y0, y1 = ad["y_range"]
    page["lines"] = [
        l for l in page["lines"]
        if not (y0 <= (l["box"][1] + l["box"][3]) / 2 <= y1 and l["box"][0] >= ad["x_min"])
    ]
    _drop_accused_gt(gt, ad["gt_index"])
    notes.append(f"dropped accused #{ad['gt_index'] + 1}")


def build_variant(spec: dict, base_gt: dict, seed: int, base_pdf=None, base_ocr=None) -> Variant:
    rng = random.Random(seed * 7919 + 13)
    variables = generate_variables(spec, seed)
    rules = compile_rules(spec, variables)
    gt = copy.deepcopy(base_gt)
    gt = _map_strings(gt, rules.apply)
    for path, template in spec.get("gt", {}).items():
        value = [_render(t, variables) for t in template] if isinstance(template, list) else _render(template, variables)
        _set_path(gt, path, value)
    notes: List[str] = []
    variant = Variant(seed, variables, gt)

    # Structural edits first, on the original text (added entries get their own
    # values from the add_template), then the global substitution.
    if base_pdf is not None:
        pdf = copy.deepcopy(base_pdf)
        protected: set = set()  # added rows already carry their final values
        _apply_table_rows(pdf, spec, rules, variables, rng, gt, notes, protected)
        _apply_accused_drop(pdf, spec, rng, gt, notes)
        for p in pdf.pages:
            p.text = rules.apply(p.text)
            for ln, new in zip(p.lines, apply_to_lines(rules, [ln.text for ln in p.lines])):
                ln.text = new
            for t in p.tables:
                t.rows = [row if id(row) in protected else [rules.apply(c) for c in row] for row in t.rows]
        variant.pdf_result = pdf
    if base_ocr is not None:
        pages = copy.deepcopy(base_ocr)
        _apply_ocr_bands(pages, spec, rules, variables, rng, gt, notes)
        _apply_ocr_accused_drop(pages, spec, rng, gt, notes)
        for p in pages:
            todo = [l for l in p["lines"] if not l.pop("_protected", False)]
            for l, new in zip(todo, apply_to_lines(rules, [l["text"] for l in todo])):
                l["text"] = new
        variant.ocr_pages = pages
    variant.description = "; ".join(notes) or "values only"
    return variant


def load_spec(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
