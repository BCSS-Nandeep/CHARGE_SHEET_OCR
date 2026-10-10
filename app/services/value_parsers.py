"""Typed value parsing shared by mapping and validation."""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}

_NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,2})\s*[./\-]\s*(\d{1,2})\s*[./\-]\s*(\d{4}|\d{2})(?!\d)")
_TEXT_DATE = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})(?!\d)")


def _year(raw: str) -> int:
    y = int(raw)
    if len(raw) == 2:
        y += 2000 if y < 50 else 1900
    return y


def parse_date(text: Optional[str]) -> Optional[date]:
    """First calendar-valid day-first date in the text (Indian convention dd.mm.yyyy)."""
    if not text:
        return None
    for m in _NUMERIC_DATE.finditer(text):
        try:
            return date(_year(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            continue
    for m in _TEXT_DATE.finditer(text):
        month = _MONTHS.get(m.group(2).lower())
        if not month:
            continue
        try:
            return date(int(m.group(3)), month, int(m.group(1)))
        except ValueError:
            continue
    return None


def has_date_shape(text: Optional[str]) -> bool:
    return bool(text and (_NUMERIC_DATE.search(text) or _TEXT_DATE.search(text)))


def digits(text: Optional[str]) -> str:
    return re.sub(r"\D", "", text or "")


def is_valid_indian_mobile(text: Optional[str]) -> bool:
    d = digits(text)
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    return len(d) == 10 and d[0] in "6789"


def is_valid_aadhaar_shape(text: Optional[str]) -> bool:
    d = digits(text)
    return len(d) == 12 and d[0] not in "01"
