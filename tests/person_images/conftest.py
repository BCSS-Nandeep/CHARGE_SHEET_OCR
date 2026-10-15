"""Fixtures for the person-image integration tests (the existing tests/conftest.py is reused, not edited)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("cv2", reason="opencv-python-headless is required for the person-image integration")

from integrations.person_images import extractor, report_result, service  # noqa: E402
from integrations.person_images.wiring import attach  # noqa: E402
from tests.conftest import ADMIN_KEY  # noqa: E402
from tests.person_images.helpers import fake_detect  # noqa: E402

# the folder the application would really use (what must never be touched by tests)
REAL_OUTPUT = Path(__file__).resolve().parents[2] / "output"


def _listing() -> set:
    return {p.name for p in REAL_OUTPUT.iterdir()} if REAL_OUTPUT.is_dir() else set()


@pytest.fixture(scope="session", autouse=True)
def _real_output_is_left_alone():
    """Fail the run if any test wrote into the real output/ folder (tests use a temporary one)."""
    before = _listing()
    yield
    after = _listing()
    assert after == before, f"tests wrote into the real output folder: {sorted(after - before)}"


@pytest.fixture()
def fake_faces(monkeypatch):
    """Replace only the face decision (see helpers.fake_detect); everything else runs for real."""
    monkeypatch.setattr(extractor, "detect_faces", fake_detect)


@pytest.fixture()
def out_root(tmp_path, monkeypatch):
    root = tmp_path / "output"
    monkeypatch.setenv("IR_PERSON_IMAGES_OUTPUT", str(root))
    yield root
    # let every background job of this test finish BEFORE the environment override is undone, so that nothing
    # started by this test can write to the real output folder afterwards
    try:
        service.wait_idle()
    except Exception:  # noqa: BLE001
        pass
    for t in threading.enumerate():
        if t.name.startswith(("person-images-hook", "person-images-watch")):
            t.join(timeout=15)
    service.wait_idle()


@pytest.fixture()
def integrated(make_client, out_root):
    """The OCR app from the existing fixtures with the integration attached (client already holds the admin key)."""
    raw = make_client(enable_auth=True)
    attach(raw.app)
    with raw as client:
        client.headers.update({"X-API-Key": ADMIN_KEY})
        yield client
