"""Same-template / different-data regression for the known templates.

Each known template is turned into many variants with different names, places,
ages, phone numbers, FIR numbers, dates, sections and witness/accused counts
(see evaluation/variants.py). Every variant must match its own ground truth
except for the template's documented known gaps.

Needs the private fixtures (variant specs + source PDFs); skipped otherwise.
Set VARIANT_SEEDS to run more variants (default 20 per template).
"""

from __future__ import annotations

import os

import pytest

from evaluation.variant_runner import SPEC_DIR

SEEDS = int(os.environ.get("VARIANT_SEEDS", "20"))
pytestmark = pytest.mark.skipif(
    not SPEC_DIR.is_dir() or not any(SPEC_DIR.glob("*.json")), reason="private variant specs not available"
)


@pytest.fixture(scope="module")
def benches():
    os.environ["OCR_ENABLED"] = "true"  # scanned template replays cached OCR boxes
    from app.config import get_settings

    get_settings.cache_clear()
    from evaluation.variant_runner import load_benches

    found = load_benches()
    if not found:
        pytest.skip("source PDFs for the variant specs are not available")
    return found


def test_known_templates_with_different_data(benches):
    failures = []
    for bench in benches:
        for seed in range(1, SEEDS + 1):
            from evaluation.variant_runner import run_variant

            outcome = run_variant(bench, seed)
            if outcome.unexpected:
                failures.append(f"{bench.template_id} seed {seed} ({outcome.description}): {outcome.unexpected}")
    assert not failures, "\n".join(failures)
