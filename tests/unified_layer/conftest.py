"""Fixtures for the unified-layer tests (the existing tests/conftest.py is reused, not edited).

* ``unified`` - the real IR_OCR host (isolated temp DB/uploads, as in the existing tests) behind the real gateway,
  with the charge-sheet side served by ``FakeWorker`` (no OCR stack needed);
* ``client`` - a TestClient on the gateway that already holds the admin API key.

Nothing here writes into the real ``output/`` or ``data/`` folders: a session guard fails the run if anything did.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("cv2", reason="opencv-python-headless is required for the person-image pipeline")

from integrations.person_images import extractor  # noqa: E402
from tests.conftest import ADMIN_KEY  # noqa: E402
from tests.person_images.helpers import fake_detect  # noqa: E402
from tests.unified_layer.fake_worker import FakeWorker  # noqa: E402
from unified import images as unified_images  # noqa: E402
from unified.adapters.worker import WorkerManager  # noqa: E402
from unified.config import load_settings  # noqa: E402
from unified.factory import create_gateway  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
REAL_DIRS = [REPO / "output", REPO / "data" / "unified"]


def pytest_configure(config):
    config.addinivalue_line("markers", "real_worker: runs the real vendored CHARGE_SHEET_OCR as a process "
                                       "(OCR and the vision model off); deselect with -m 'not real_worker'")


def _listing(path: Path) -> set:
    return {p.name for p in path.iterdir()} if path.is_dir() else set()


@pytest.fixture(scope="session", autouse=True)
def _real_folders_are_left_alone():
    before = {str(d): _listing(d) for d in REAL_DIRS}
    yield
    after = {str(d): _listing(d) for d in REAL_DIRS}
    assert after == before, f"tests wrote into a real folder: { {k: sorted(after[k] - before[k]) for k in after} }"


@pytest.fixture()
def fake_faces(monkeypatch):
    """Replace only the face decision (red-dominant image = person); extraction, storage and statuses run for real."""
    monkeypatch.setattr(extractor, "detect_faces", fake_detect)


@pytest.fixture()
def out_root(tmp_path, monkeypatch):
    root = tmp_path / "output"
    monkeypatch.setenv("IR_PERSON_IMAGES_OUTPUT", str(root))
    yield root
    # let background jobs of this test finish BEFORE the override is undone
    try:
        unified_images.wait_idle()
    except Exception:  # noqa: BLE001
        pass
    for t in threading.enumerate():
        if t.name.startswith(("unified-watch", "unified-followup")):  # not the image pool's long-lived thread
            t.join(timeout=15)
    unified_images.wait_idle()


@pytest.fixture()
def fake_worker():
    worker = FakeWorker()
    worker.url = worker.start()
    yield worker
    worker.stop()


@pytest.fixture()
def build_unified(make_client, out_root, fake_worker, tmp_path):
    """Factory: the real IR_OCR host (auth on or off) behind the gateway, charge sheets served by the fake worker."""
    built = []

    def factory(enable_auth: bool = True):
        host = make_client(enable_auth=enable_auth).app
        settings = load_settings({
            "UNIFIED_DATA_DIR": str(tmp_path / "unified"), "CS_WORKER_URL": fake_worker.url,
            "CS_WORKER_MANAGE": "false", "CS_WORKER_AUTOSTART": "false", "CLASSIFY_OCR": "false",
        })
        gateway = create_gateway(settings, host_app=host, manager=WorkerManager(settings))
        built.append(gateway)
        return SimpleNamespace(gateway=gateway, host=host, worker=fake_worker, settings=settings,
                               coordinator=gateway.coordinator, registry=gateway.coordinator.registry,
                               out_root=out_root)

    yield factory
    for gateway in built:   # stop this test's OCR watchers before the fake worker goes away
        gateway.coordinator.report.shutdown()


@pytest.fixture()
def unified(build_unified):
    return build_unified(enable_auth=True)


@pytest.fixture()
def client(unified):
    with TestClient(unified.gateway) as c:
        c.headers.update({"X-API-Key": ADMIN_KEY})
        yield c


def wait_for(predicate, timeout=20.0, step=0.05):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not reached in time")
