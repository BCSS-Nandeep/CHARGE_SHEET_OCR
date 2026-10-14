"""Text matching shared by the validator and the evidence index (no other imports from the package)."""

from __future__ import annotations

import difflib
import re
from typing import Any, Iterable, Optional

_NIL = {"", "nil", "-", "--", "---", "n/a", "na", "none", "null", "........", "…"}
_SNAP_CUTOFF = 0.82


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "", str(text).lower())


def _strip_yes(text: str) -> str:
    """'Black: Yes' / 'Normal Yeas' -> 'Black' / 'Normal' (a marking, not part of the option)."""
    return re.sub(r"[\s:;,\-]*\b(yes|yeas|yess|y)\b\W*$", "", str(text), flags=re.IGNORECASE).strip()


def is_blank(value: Any) -> bool:
    return value is None or str(value).strip().lower() in _NIL


def _relation_key(text: Any) -> str:
    """'Father’s Sister' / "Fathers sisters" -> 'fatherssister': letters only, plural-insensitive."""
    k = re.sub(r"[^a-z0-9]+", "", str(text).lower())
    return k[:-1] if k.endswith("s") and len(k) > 3 else k


def snap_relation(raw: Any, relations: Iterable[str]) -> Optional[str]:
    """The family relation a printed label names, by exact match only (spelling of the apostrophe and a
    plural 's' ignored). 'Father's Sister' is never 'Father' or 'Sister'; an unknown label returns None
    so the row is kept as printed instead of being forced onto another relation."""
    if raw is None:
        return None
    key = _relation_key(raw)
    if not key:
        return None
    for r in relations:
        if _relation_key(r) == key:
            return r
    return None


def snap_option(raw: Any, options: Iterable[str]) -> Optional[str]:
    """Return the canonical option that ``raw`` means, or None."""
    if raw is None:
        return None
    opts = list(options)
    for candidate in (str(raw), _strip_yes(str(raw))):
        n = _norm(candidate)
        if not n:
            continue
        by_norm = {_norm(o): o for o in opts}
        if n in by_norm:
            return by_norm[n]
        # containment only for reasonably long tokens (avoid 'long' matching 'Long Hair')
        for on, o in by_norm.items():
            if len(on) >= 5 and (on in n or (len(n) >= 5 and n in on)):
                return o
        close = difflib.get_close_matches(n, list(by_norm), n=1, cutoff=_SNAP_CUTOFF)
        if close:
            return by_norm[close[0]]
    return None
