"""Turn the VLM's page transcription (plain text + HTML tables + tags) into lines and rows.

The model returns text as it reads the page, tables as ``<table>`` HTML, and wraps
watermarks, page numbers and image descriptions in tags. This module keeps three
views of the same page:

* ``raw_lines``    — every line as read (tags kept), for the raw_json view;
* ``items``        — free lines and table rows in reading order, for the as-printed
                     field builder (watermark / page-number / image text left out);
* ``mapping_text`` — plain text for the structured-mapping prompt (table rows as
                     ``cell | cell``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import List, Tuple, Union

_NOISE_TAGS = ("watermark", "page_number", "img")
_NOISE = re.compile(r"<(%s)>.*?</\1>" % "|".join(_NOISE_TAGS), re.S | re.I)
_TABLE = re.compile(r"<table\b.*?</table>", re.S | re.I)
_ANY_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_WS = re.compile(r"[ \t ]+")


@dataclass
class ParsedPage:
    raw_lines: List[str] = field(default_factory=list)
    # ("line", text) | ("row", [cells])
    items: List[Tuple[str, Union[str, List[str]]]] = field(default_factory=list)
    mapping_text: str = ""

    @property
    def char_count(self) -> int:
        return len(self.mapping_text.strip())


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[str]] = []
        self._row: List[str] = []
        self._cell: List[str] = []
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._in_cell, self._cell = True, []
        elif tag == "br" and self._in_cell:
            self._cell.append("\n")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in_cell:
            self._row.append(_clean_cell("".join(self._cell)))
            self._in_cell = False
        elif tag == "tr":
            if any(c for c in self._row):
                self.rows.append(self._row)
            self._row = []

    def handle_data(self, data):
        if self._in_cell:
            self._cell.append(data)


def _clean_cell(text: str) -> str:
    parts = [_WS.sub(" ", p).strip() for p in text.split("\n")]
    return " ".join(p for p in parts if p)


def table_rows(html: str) -> List[List[str]]:
    """Rows of one ``<table>`` as non-empty cell texts (empty cells dropped, order kept)."""
    parser = _TableParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return []
    return [[c for c in row if c] for row in parser.rows if any(row)]


def parse_transcription(raw: str) -> ParsedPage:
    page = ParsedPage()
    if not raw or not raw.strip():
        return page
    pos = 0
    chunks: List[Tuple[str, str]] = []  # ("text"|"table", content)
    for m in _TABLE.finditer(raw):
        if m.start() > pos:
            chunks.append(("text", raw[pos : m.start()]))
        chunks.append(("table", m.group(0)))
        pos = m.end()
    if pos < len(raw):
        chunks.append(("text", raw[pos:]))

    mapping: List[str] = []
    for kind, content in chunks:
        if kind == "text":
            for ln in content.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                page.raw_lines.append(ln)
                clean = _WS.sub(" ", _ANY_TAG.sub("", _NOISE.sub("", ln))).strip()
                if clean:
                    page.items.append(("line", clean))
                    mapping.append(clean)
        else:
            rows = table_rows(content)
            if not rows:
                page.raw_lines.append(_WS.sub(" ", _ANY_TAG.sub(" ", content)).strip())
                continue
            for row in rows:
                page.raw_lines.append(" | ".join(row))
                page.items.append(("row", row))
                mapping.append(" | ".join(row))
    page.mapping_text = "\n".join(mapping)
    return page


def split_for_mapping(text: str, max_chars: int) -> List[str]:
    """Split page text at line boundaries into chunks of at most ``max_chars`` (one chunk
    when it fits). A single line longer than the limit becomes its own chunk."""
    if len(text) <= max_chars:
        return [text]
    chunks: List[str] = []
    cur: List[str] = []
    size = 0
    for ln in text.splitlines():
        if cur and size + len(ln) + 1 > max_chars:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks
