"""Unit tests: registry, worker manager, adapter state, pure routing rules, and the integrity checker itself."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from unified import registry as reg
from unified.adapters.base import AdapterUnavailable, NativeState, ocr_status
from unified.adapters.charge_sheet import ChargeSheetAdapter
from unified.adapters.worker import ENTRY_DIR, WorkerManager
from unified.config import ROOT, load_settings
from unified.gateway import classify_request, followup
from unified.routes import inject_assets, required_role

CS = "charge_sheet_ocr"
ID = "11111111-2222-3333-4444-555555555555"


# -------------------------------------------------------------------------------------------- registry
def make_registry(tmp_path):
    return reg.Registry(tmp_path / "registry.db")


def add(r, doc_id=ID, adapter=CS, **kw):
    r.add_document(document_id=doc_id, intake_id=None, original_filename="a.pdf", stored_original="x_a.pdf",
                   detected_kind="pdf", content_type="application/pdf", size_bytes=3, sha256="ab", document_type="charge_sheet",
                   type_source="auto", adapter=adapter, native_id=doc_id, classification={"decision": "auto"},
                   uploaded_by="u", **kw)


def test_registry_round_trip_and_ownership(tmp_path):
    r = make_registry(tmp_path)
    assert r.adapter_of(ID) is None and r.get_document(ID) is None          # unknown ids are left to the host
    add(r)
    assert r.adapter_of(ID) == CS and r.get_document(ID)["classification"] == {"decision": "auto"}
    r.mark_deleted(ID)
    assert r.get_document(ID)["deleted"] is True and r.list_documents() == [] and len(r.list_documents(include_deleted=True)) == 1


def test_registry_events_are_kept_in_order(tmp_path):
    r = make_registry(tmp_path)
    r.add_event(ID, "warning", "classification", "w1")
    r.add_event(ID, "error", "person_images", "e1")
    assert [(e["level"], e["message"]) for e in r.events(ID)] == [("warning", "w1"), ("error", "e1")]


def test_an_intake_can_be_claimed_by_only_one_request(tmp_path):
    r = make_registry(tmp_path)
    intake = r.create_intake(original_filename="a.pdf", stored_original="x", detected_kind="pdf", content_type=None,
                             size_bytes=1, sha256="s", classification={}, uploaded_by=None)
    wins = []
    barrier = threading.Barrier(8)

    def claim():
        barrier.wait()
        wins.append(r.claim_intake(intake))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1


@pytest.mark.parametrize("name", ["../../evil.pdf", "C:\\Windows\\system32\\x.pdf", "a b c?.pdf", "", "…", "con.pdf"])
def test_stored_names_are_always_a_plain_file_name(name):
    stored = reg.stored_name(name, ".pdf")
    assert stored == Path(stored).name and "/" not in stored and "\\" not in stored and stored.endswith(".pdf")


# ------------------------------------------------------------------------------------ status mapping
@pytest.mark.parametrize("doc,ext,expected", [
    ("queued", None, "processing"), ("processing", None, "processing"), ("uploaded", None, "processing"),
    ("failed", None, "failed"), ("completed", "complete", "succeeded"), ("verified", "complete", "succeeded"),
    ("completed", "partial", "partial"), ("completed", "failed", "failed"), ("deleted", None, "unknown"), (None, None, "unknown"),
])
def test_ocr_status_mapping(doc, ext, expected):
    assert ocr_status(doc, ext) == expected


def test_native_state_finality():
    assert NativeState(document_status="completed").final and NativeState(document_status="failed").final
    assert not NativeState(document_status="processing").final and not NativeState(document_status="queued").final
    assert NativeState(exists=False).final


# ------------------------------------------------------------------------------------ worker manager
def settings_for(tmp_path, **extra):
    return load_settings({"UNIFIED_DATA_DIR": str(tmp_path / "u"), "CS_WORKER_URL": "http://127.0.0.1:59999",
                          "CS_WORKER_MANAGE": "true", **extra})


def host_settings(**kw):
    from types import SimpleNamespace

    base = dict(enable_auth=False, api_key="", api_keys="", api_key_header="X-API-Key")
    return SimpleNamespace(**{**base, **kw})


def test_worker_environment_is_isolated_from_the_host(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))                    # the host's path must not leak into the worker
    m = WorkerManager(settings_for(tmp_path))
    monkeypatch.setattr(m, "_host_settings", lambda: host_settings())
    env = m.worker_env()
    assert env["PYTHONPATH"] == str(ENTRY_DIR) and str(ROOT) not in env["PYTHONPATH"].split(os.pathsep)
    assert Path(env["UPLOAD_DIR"]).is_relative_to(tmp_path) and env["DATABASE_URL"].startswith("sqlite:///")
    assert str(tmp_path).replace("\\", "/") in env["DATABASE_URL"]
    assert env["ENABLE_AUTH"] == "false" and env["APP_HOST"] == "127.0.0.1" and env["APP_PORT"] == "59999"
    assert m.internal_headers() == {}


def test_worker_gets_the_hosts_keys_plus_a_private_read_only_key_when_auth_is_on(tmp_path, monkeypatch):
    m = WorkerManager(settings_for(tmp_path))
    hs = host_settings(enable_auth=True, api_key="admin-key-0123456789", api_keys="ops:operator-key-0123456789:operator")
    monkeypatch.setattr(m, "_host_settings", lambda: hs)
    env = m.worker_env()
    assert env["ENABLE_AUTH"] == "true" and env["API_KEY"] == "admin-key-0123456789"
    assert "ops:operator-key-0123456789:operator" in env["API_KEYS"] and ":viewer" in env["API_KEYS"]
    internal = m.internal_headers()["X-API-Key"]
    assert len(internal) >= 16 and f"unified-internal:{internal}:viewer" in env["API_KEYS"]


def test_unreachable_unmanaged_worker_is_a_clear_error(tmp_path):
    m = WorkerManager(settings_for(tmp_path, CS_WORKER_MANAGE="false"))
    with pytest.raises(AdapterUnavailable, match="not reachable"):
        m.ensure_running()


def test_missing_interpreter_or_snapshot_is_a_clear_error_not_a_crash(tmp_path):
    m = WorkerManager(settings_for(tmp_path, CS_WORKER_PYTHON=str(tmp_path / "nope" / "python.exe")))
    with pytest.raises(AdapterUnavailable, match="CS_WORKER_PYTHON"):
        m.ensure_running()
    m = WorkerManager(settings_for(tmp_path, CS_WORKER_DIR=str(tmp_path / "nowhere")))
    with pytest.raises(AdapterUnavailable, match="snapshot not found"):
        m.ensure_running()


def test_worker_exits_when_its_parent_disappears(tmp_path):
    """A killed application runs no shutdown code; its managed worker must not be left behind holding the port."""
    import socket
    import time

    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    m = WorkerManager(settings_for(tmp_path, CS_WORKER_URL=f"http://127.0.0.1:{port}", CS_WORKER_PYTHON=sys.executable))
    monkey_env = m.worker_env()
    monkey_env.update({"UNIFIED_PARENT_PID": str(parent.pid), "OCR_ENABLED": "false", "VLM_ENABLED": "false",
                       "DATABASE_URL": "sqlite:///" + (tmp_path / "w.db").as_posix(), "UPLOAD_DIR": str(tmp_path)})
    child = subprocess.Popen([sys.executable, "-m", "uvicorn", "cs_worker_entry:app", "--port", str(port)],
                             cwd=str(m.settings.worker_dir), env=monkey_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 60
        while time.time() < deadline and not WorkerManager(settings_for(tmp_path, CS_WORKER_URL=f"http://127.0.0.1:{port}")).healthy(1):
            time.sleep(0.5)
        assert child.poll() is None                                # up and running
        parent.kill()
        parent.wait()
        child.wait(timeout=30)                                     # ... and gone once its parent is
    finally:
        for p in (parent, child):
            if p.poll() is None:
                p.kill()


def test_worker_entry_adds_one_route_and_keeps_the_snapshot_untouched():
    src = (ENTRY_DIR / "cs_worker_entry.py").read_text(encoding="utf-8")
    assert "from app.main import app" in src and src.count("include_router") == 1


# ---------------------------------------------------------------------------------- adapter state
def test_charge_sheet_adapter_state_follows_the_worker(fake_worker, tmp_path):
    adapter = ChargeSheetAdapter(WorkerManager(settings_for(tmp_path, CS_WORKER_URL=fake_worker.url, CS_WORKER_MANAGE="false")))
    fake_worker.mode = "hold"
    import httpx

    doc_id = httpx.post(f"{fake_worker.url}/api/v1/documents/upload", files={"file": ("a.pdf", b"%PDF-1.4 x", "application/pdf")}).json()["document_id"]
    s = adapter.state(doc_id)
    assert (s.ocr_status, s.document_status, s.payload) == ("processing", "queued", None) and s.progress["step"] == "ocr"
    fake_worker.release(doc_id)
    s = adapter.state(doc_id)
    assert s.ocr_status == "succeeded" and s.payload["results"]["schema_version"] == "2.0.0" and s.schema_version == "2.0.0"
    fake_worker.release(doc_id, "failed")
    s = adapter.state(doc_id)
    assert s.ocr_status == "failed" and s.payload is None and "simulated" in s.error
    assert adapter.state("00000000-0000-0000-0000-000000000000").exists is False
    fake_worker.stop()
    gone = adapter.state(doc_id)
    assert gone.reachable is False and gone.ocr_status == "unknown" and gone.exists   # an outage is not a deletion


def test_adapters_declare_what_they_accept(unified):
    c = unified.coordinator
    assert c.ir.supported_kinds == {"pdf", "docx"}                                  # what IR_OCR's upload accepts
    assert c.cs.supported_kinds == {"pdf", "docx", "png", "jpeg", "tiff"}           # what CHARGE_SHEET_OCR accepts
    assert c.by_type == {"interrogation_report": c.ir, "charge_sheet": c.cs}
    assert c.ir.provenance()["commit"] == "004a1eef5bfc8eba3487d595326a46561595f2e3"
    assert c.cs.provenance()["commit"] == "e6d6dd6c1213bd91895f959c98aed51723ebf9a1"


# ------------------------------------------------------------------------------------ pure routing
@pytest.mark.parametrize("method,path,owner,expected", [
    ("GET", "/", None, "unified"), ("POST", "/api/v1/documents/upload", None, "unified"),
    ("GET", "/api/v1/documents", None, "unified"), ("GET", "/api/v1/unified/health", None, "unified"),
    ("GET", "/unified-static/unified.js", None, "unified"), ("GET", "/cs-static/js/app.js", None, "unified"),
    ("GET", f"/api/v1/documents/{ID}/report_result", None, "unified"),
    ("GET", f"/api/v1/documents/{ID}/person-images/files/run_x/a.png", "ir_ocr", "unified"),
    ("POST", f"/api/v1/documents/{ID}/person-images/run", CS, "unified"),
    ("GET", f"/api/v1/documents/{ID}/results", CS, "unified"), ("GET", f"/api/v1/documents/{ID}", CS, "unified"),
    ("GET", f"/api/v1/documents/{ID}/results", "ir_ocr", "host"), ("GET", f"/api/v1/documents/{ID}/results", None, "host"),
    ("GET", f"/results/{ID}", CS, "unified"), ("GET", f"/results/{ID}", "ir_ocr", "host_results"),
    ("GET", f"/results/{ID}", None, "host_results"), ("GET", "/history", None, "host"), ("GET", "/health", None, "host"),
    ("GET", "/static/js/app.js", None, "host"), ("GET", "/docs", None, "host"),
    ("POST", "/api/v1/documents/upload/extra", None, "host"),
])
def test_request_routing_table(method, path, owner, expected):
    assert classify_request(method, path, lambda _id: owner, CS)[0] == expected


@pytest.mark.parametrize("method,path,expected", [
    ("POST", f"/api/v1/documents/{ID}/reprocess", "reprocess"), ("PATCH", f"/api/v1/documents/{ID}/fields", "change"),
    ("POST", f"/api/v1/documents/{ID}/verify", "change"), ("DELETE", f"/api/v1/documents/{ID}", "delete"),
    ("GET", f"/api/v1/documents/{ID}/fields", None), ("GET", f"/api/v1/documents/{ID}", None),
    ("POST", "/api/v1/documents/upload", None), ("DELETE", f"/api/v1/documents/{ID}/other", None),
])
def test_followup_table(method, path, expected):
    assert followup(method, path)[0] == expected


@pytest.mark.parametrize("method,rest,role", [
    ("GET", "results", "viewer"), ("GET", "source", "viewer"), ("PATCH", "fields", "operator"),
    ("POST", "reprocess", "operator"), ("POST", "verify", "reviewer"), ("POST", "verify/", "reviewer"),
    ("DELETE", "", "admin"), ("POST", "anything", "operator"),
])
def test_forwarded_calls_need_the_same_role_as_in_the_service(method, rest, role):
    assert required_role(method, rest) == role


def test_asset_injection_is_idempotent_and_only_touches_head_and_body():
    html = b"<html><head><title>t</title></head><body><div id='x'></div></body></html>"
    once = inject_assets(html)
    assert once.count(b"unified_images.js") == 1 and once.count(b"unified.css") == 1
    assert inject_assets(once) == once
    assert once.replace(b"</head>", b"").replace(b"</body>", b"").count(b"<div id='x'></div>") == 1


# --------------------------------------------------------------------------- integrity checker works
def load_checker():
    spec = importlib.util.spec_from_file_location("verify_integrity", ROOT / "scripts" / "verify_integrity.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_real_repository_passes_the_integrity_check():
    checker = load_checker()
    assert checker.check_vendor(None) == []                       # vendored snapshot == the CHARGE_SHEET_OCR commit
    if shutil.which("git") and subprocess.run(["git", "cat-file", "-e", checker.json.loads(
            checker.PROVENANCE.read_text())["ir_ocr"]["commit"]], cwd=ROOT).returncode == 0:
        assert checker.check_host() == []                          # IR_OCR files == the IR_OCR base commit


def test_integrity_check_detects_a_changed_edited_or_missing_vendored_file(tmp_path, monkeypatch):
    checker = load_checker()
    copy = tmp_path / "vendor"
    shutil.copytree(checker.VENDOR, copy, ignore=shutil.ignore_patterns("__pycache__", ".env"))
    monkeypatch.setattr(checker, "VENDOR", copy)
    assert checker.check_vendor(None) == []
    victim = copy / "app" / "services" / "field_builder.py"
    victim.write_bytes(victim.read_bytes() + b"\n# retuned\n")
    (copy / "app" / "services" / "page_router.py").unlink()
    (copy / "app" / "services" / "extra.py").write_text("x = 1\n")
    problems = "\n".join(checker.check_vendor(None))
    assert "differs" in problems and "field_builder.py" in problems
    assert "missing" in problems and "page_router.py" in problems
    assert "unexpected" in problems and "extra.py" in problems


@pytest.mark.skipif(not shutil.which("git"), reason="git is required")
def test_integrity_check_detects_a_changed_host_file(tmp_path, monkeypatch):
    checker = load_checker()
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "ocr.py").write_text("x = 1\n")
    run("add", ".")
    run("commit", "-q", "-m", "base")
    head = run("rev-parse", "HEAD")
    prov = tmp_path / "provenance.json"
    prov.write_text(json.dumps({"ir_ocr": {"commit": head}, "charge_sheet_ocr": {"commit": "x"}}))
    monkeypatch.setattr(checker, "ROOT", repo)
    monkeypatch.setattr(checker, "PROVENANCE", prov)
    (repo / "new_file.py").write_text("added = True\n")             # additions are allowed
    run("add", "new_file.py")
    assert checker.check_host() == []
    (repo / "ocr.py").write_text("x = 2\n")                          # a retuned OCR file is not
    assert any("ocr.py" in p for p in checker.check_host())
