"""Spacing softener / repair — restore missing spaces; do not invent text."""

from __future__ import annotations

import pytest

from app.services.text_normalize import (
    normalize_doc_text,
    repair_spacing_artifacts,
    restore_missing_spaces,
    soften_pdf_text,
)


@pytest.mark.parametrize(
    "broken,expected",
    [
        ("07: 30", "07:30"),
        ("P 8", "P8"),
        ("M 11", "M11"),
        ("P 1, P 2, P 3", "P1, P2, P3"),
        ("PC. 1460", "PC.1460"),
        ("contact s/facilitators", "contacts/facilitators"),
        ("G.  Srinivas", "G. Srinivas"),
        ("MD 634 KE 44 B 2 A58303", "MD634KE44B2A58303"),
        ("OE 4 AB 2225978", "OE4AB2225978"),
    ],
)
def test_repair_identifier_gaps(broken, expected):
    assert repair_spacing_artifacts(broken) == expected


@pytest.mark.parametrize(
    "glued,expected_substr",
    [
        ("isDosapatiKalyan", "Dosapati Kalyan"),
        ("DosapatiVamshi", "Dosapati Vamshi"),
        ("JillaManoj", "Jilla Manoj"),
        ("GogulothManoj", "Goguloth Manoj"),
        ("6kgs", "6 kgs"),
        ("600grams", "600 grams"),
        ("Outofthe6K,Gs", "Out of the"),
        ("26.08.2025atabout14:30hours", "26.08.2025"),
        ("packet.seized", "packet. seized"),
        ("him.Previosuly", "him. Previosuly"),  # no spelling invention
        ("since3daysago", "since 3"),
    ],
)
def test_restore_missing_spaces(glued, expected_substr):
    out = restore_missing_spaces(glued)
    assert expected_substr in out
    # Never invent or drop alphanumeric characters
    assert "".join(ch for ch in out if ch.isalnum()).lower() == "".join(
        ch for ch in glued if ch.isalnum()
    ).lower()


def test_soften_round_trip_identifiers():
    for original in ("07:30", "P8", "M11", "PC.1460", "MD634KE44B2A58303"):
        out = soften_pdf_text(original)
        assert original in out


def test_soften_splits_glued_prose_and_keeps_markers():
    text = "isDosapatiKalyan with P8 at 07:30 and 6kgs ganja"
    out = soften_pdf_text(text)
    assert "Dosapati Kalyan" in out
    assert "P8" in out
    assert "07:30" in out
    assert "6 kgs" in out


def test_normalize_doc_restores_and_preserves():
    out = normalize_doc_text("JillaManoj seized P8 at 14:30hours")
    assert "Jilla Manoj" in out
    assert "P8" in out
    assert "14:30" in out
    assert "hours" in out


def test_no_spelling_invention():
    assert "Previosuly" in soften_pdf_text("him.Previosuly")
    assert "Previously" not in soften_pdf_text("him.Previosuly")
