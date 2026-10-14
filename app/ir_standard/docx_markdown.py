"""DOCX -> markdown that keeps the marks a reviewer relies on.

Bold runs become ``**bold**``, ticks (Wingdings symbols, Word checkbox content
controls, or ☑/✓/✔ characters) become ``☑``, and bulleted options without a
tick become ``☐``. Tables become markdown tables. The VLM then maps this text
onto the standard template, so a native Word report needs no OCR and loses no
formatting. Scanned reports produce the same markdown from the VLM instead.
"""

from __future__ import annotations

import re
import threading
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
W14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"

TICK = "☑"  # ☑
BOX = "☐"  # ☐

# Wingdings / Wingdings 2 code points that draw a tick or a checked box
_TICK_SYMS = {"F050", "F051", "F052", "F053", "F0FC", "F0FD", "F0FE", "F0D6", "F0FB"}
_BOX_SYMS = {"F0A8", "F06F", "F071", "F0A3", "F0A1", "F0A0"}
_TICK_CHARS = set("☑☒✓✔✅✕✖")
_BOX_CHARS = set("☐□◻")


def _bold(rpr: Optional[ET.Element]) -> bool:
    if rpr is None:
        return False
    b = rpr.find(W + "b")
    return b is not None and b.get(W + "val") not in ("0", "false", "off")


def _runs(p: ET.Element) -> List[Tuple[str, bool]]:
    """Ordered (text, bold) segments of one paragraph."""
    out: List[Tuple[str, bool]] = []
    for node in p.iter():
        if node.tag == W14 + "checkbox":
            checked = node.find(W14 + "checked")
            out.append((TICK if checked is not None and checked.get(W14 + "val") in ("1", "true") else BOX, False))
        if node.tag != W + "r":
            continue
        bold = _bold(node.find(W + "rPr"))
        text = []
        for c in node:
            if c.tag == W + "t":
                text.append(c.text or "")
            elif c.tag == W + "tab":
                text.append(" ")
            elif c.tag == W + "br":
                text.append("\n")
            elif c.tag == W + "sym":
                code = (c.get(W + "char") or "").upper()
                if code in _TICK_SYMS:
                    text.append(TICK)
                elif code in _BOX_SYMS:
                    text.append(BOX)
        out.append(("".join(text), bold))
    return out


class _Numbering:
    """Word list numbering: tells bullets (option boxes) from decimal/letter lists (headings, items)."""

    def __init__(self, root: Optional[ET.Element]):
        self.abstract: dict = {}  # abstractNumId -> {ilvl: (numFmt, lvlText)}
        self.num_to_abstract: dict = {}
        self.counters: dict = {}
        if root is None:
            return
        for an in root.findall(W + "abstractNum"):
            levels = {}
            for lvl in an.findall(W + "lvl"):
                fmt = lvl.find(W + "numFmt")
                txt = lvl.find(W + "lvlText")
                levels[int(lvl.get(W + "ilvl", "0"))] = (
                    fmt.get(W + "val") if fmt is not None else "decimal",
                    txt.get(W + "val") if txt is not None else "",
                )
            self.abstract[an.get(W + "abstractNumId")] = levels
        for n in root.findall(W + "num"):
            a = n.find(W + "abstractNumId")
            if a is not None:
                self.num_to_abstract[n.get(W + "numId")] = a.get(W + "val")

    def prefix(self, num_id: str, ilvl: int) -> str:
        """'☐ ' for a bullet, '3. ' for a numbered item, '' when unknown."""
        levels = self.abstract.get(self.num_to_abstract.get(num_id, ""), {})
        fmt, _ = levels.get(ilvl, ("bullet", ""))
        if fmt in ("bullet", "none"):
            return f"{BOX} "
        key = (num_id, ilvl)
        self.counters[key] = self.counters.get(key, 0) + 1
        for k in [k for k in self.counters if k[0] == num_id and k[1] > ilvl]:
            del self.counters[k]
        n = self.counters[key]
        if fmt in ("lowerLetter", "upperLetter"):
            ch = chr(ord("a" if fmt == "lowerLetter" else "A") + (n - 1) % 26)
            return f"{ch}) "
        return f"{n}. "


_state = threading.local()  # numbering of the document being converted (per thread)


def _paragraph(p: ET.Element) -> str:
    segments: List[Tuple[str, bool]] = []
    for text, bold in _runs(p):
        for ch in text:
            if ch in _TICK_CHARS:
                ch = TICK
            elif ch in _BOX_CHARS:
                ch = BOX
            if segments and segments[-1][1] == bold:
                segments[-1] = (segments[-1][0] + ch, bold)
            else:
                segments.append((ch, bold))
    parts: List[str] = []
    for text, bold in segments:
        if bold and text.strip():
            lead = len(text) - len(text.lstrip())
            trail = len(text) - len(text.rstrip())
            parts.append(text[:lead] + "**" + text.strip() + "**" + text[len(text) - trail:])
        else:
            parts.append(text)
    line = re.sub(r"[ \t]+", " ", "".join(parts)).strip()
    ppr = p.find(W + "pPr")
    numpr = ppr.find(W + "numPr") if ppr is not None else None
    numid = numpr.find(W + "numId") if numpr is not None else None
    if numid is not None and numid.get(W + "val") not in (None, "0") and line:
        ilvl_el = numpr.find(W + "ilvl")
        ilvl = int(ilvl_el.get(W + "val", "0")) if ilvl_el is not None else 0
        prefix = getattr(_state, "numbering", _Numbering(None)).prefix(numid.get(W + "val"), ilvl)
        if prefix == f"{BOX} " and (TICK in line or BOX in line):
            prefix = ""  # a tick symbol replaces the bullet; do not add a second box
        line = prefix + line  # bullet = an unticked option box; decimal = a printed number
    return line


def _cell(tc: ET.Element) -> str:
    lines = [_paragraph(p) for p in tc.iter(W + "p")]
    return "<br>".join(x for x in lines if x).replace("|", "/")


def _table(tbl: ET.Element) -> List[str]:
    rows: List[List[str]] = []
    for tr in tbl.findall(W + "tr"):
        rows.append([_cell(tc) for tc in tr.findall(W + "tc")])
    if not rows:
        return []
    width = max(len(r) for r in rows)
    lines = []
    for i, r in enumerate(rows):
        r = r + [""] * (width - len(r))
        lines.append("| " + " | ".join(r) + " |")
        if i == 0:
            lines.append("|" + " --- |" * width)
    return lines


def _walk(parent: ET.Element, out: List[str]) -> None:
    for el in parent:
        if el.tag == W + "p":
            line = _paragraph(el)
            if line:
                out.append(line)
        elif el.tag == W + "tbl":
            out.append("")
            out.extend(_table(el))
            out.append("")
        elif el.tag == W + "sdt":
            content = el.find(W + "sdtContent")
            if content is not None:
                _walk(content, out)


def docx_to_markdown(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
        names = z.namelist()
        numbering = ET.fromstring(z.read("word/numbering.xml")) if "word/numbering.xml" in names else None
    _state.numbering = _Numbering(numbering)
    body = root.find(W + "body")
    out: List[str] = []
    if body is not None:
        _walk(body, out)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
