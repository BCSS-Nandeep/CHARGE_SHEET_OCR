"""Real inference against the configured local model server. Opt-in:

    $env:RUN_VLM_TESTS="1"; pytest tests/test_vlm_live.py -q -s

The server handles one request at a time and may be shared with other applications, so these
tests skip when it is not reachable or is busy, wait for it to be idle before each request
(VLM_WAIT_IDLE_SECONDS) and send only synthetic fixture pages (no real documents). They are slow:
one scanned page is a few minutes on a CPU server.
"""

from __future__ import annotations

import os

import pytest

from app.config import get_settings

pytestmark = pytest.mark.skipif(os.environ.get("RUN_VLM_TESTS") != "1", reason="set RUN_VLM_TESTS=1 to run real inference")


@pytest.fixture()
def live(monkeypatch):
    # tests/conftest.py disables the VLM; opt back in with the real settings from .env
    monkeypatch.setenv("VLM_ENABLED", "true")
    monkeypatch.setenv("VLM_WAIT_IDLE_SECONDS", "120")
    monkeypatch.setenv("VLM_MAX_RETRIES", "0")
    get_settings.cache_clear()
    from app.services.vlm_client import VlmClient

    client = VlmClient(get_settings())
    h = client.health()
    if not (h["reachable"] and h["model_available"]):
        pytest.skip(f"model server not usable: {h['error']}")
    if client.server_busy():
        pytest.skip("model server is busy with another request; not queueing behind it")
    return client


def test_health_confirms_model_and_image_input(live):
    h = live.health()
    assert h["model"] == get_settings().vlm_model and h["model_available"]
    assert h["supports_images"] in (True, None)


def test_real_model_reads_a_scanned_page(live, fixtures_dir):
    from app.services.pdf_service import PdfService
    from app.services.vlm_text import parse_transcription

    png = PdfService().render_page_image(fixtures_dir / "sample_04_scanned_image.pdf", 1,
                                         max_side=get_settings().vlm_max_image_side)
    reply = live.transcribe_image(png)
    parsed = parse_transcription(reply.text)
    print(f"\ntranscription: {reply.seconds}s, {reply.completion_tokens} tokens")
    assert not reply.truncated and parsed.char_count > 100
    flat = "".join(parsed.mapping_text.lower().split())
    assert "901/2021" in flat and "harbourps" in flat


def test_real_model_maps_a_digital_page_to_valid_structured_json(live, fixtures_dir):
    from app.schemas.structured import ValueStatus
    from app.services.extraction_service import ExtractionService

    # the whole pipeline on a 3-page digital document: every page is mapped by the real model
    data = ExtractionService().process(fixtures_dir / "sample_03_multipage.pdf")
    s = data.structured
    assert s is not None and s.document_status.value in ("extracted", "partial")
    fir = s.single["case"]["fir_number"]
    assert fir.status == ValueStatus.EXTRACTED and fir.value.replace(" ", "") == "55/2022" and fir.source_page == 1
    # the document has no FIR date: whatever the model says, it is never presented as extracted
    assert s.single["case"]["fir_date"].status in (ValueStatus.NOT_FOUND, ValueStatus.UNVERIFIED, ValueStatus.AMBIGUOUS)
    assert s.validation.is_valid, [i.model_dump() for i in s.validation.issues if i.severity == "error"]
    names = [r.fields["name"].value for r in s.lists["accused"]]
    print("\naccused found:", len(names), "; page timings:", [(p.page, p.map_seconds) for p in s.pages])
    assert len(names) >= 2
