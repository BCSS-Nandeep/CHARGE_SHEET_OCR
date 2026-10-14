"""Check the review UI against the API in a real browser (headless Microsoft Edge / Chrome).

Seeds differently shaped documents using a *fake* model (tests/fake_vlm.py — no real server is
contacted), serves the app on a local port, drives the browser through the DevTools protocol and
compares what the page shows with the API JSON, then exercises the interactions: filter, jump to
the source page, "use this" on a conflicting value, editing + saving a correction.

    python scripts/verify_ui.py              # prints a PASS/FAIL line per check
    python scripts/verify_ui.py --shots out  # also saves screenshots to the folder

Needs Edge or Chrome (set BROWSER_EXE to use another). It tests the UI only: it says nothing about
how accurate the real model is.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="cs_ui_"))
os.environ.update({
    "DATABASE_URL": f"sqlite:///{TMP / 'ui.db'}", "UPLOAD_DIR": str(TMP / "uploads"), "ENABLE_AUTH": "false",
    "ASYNC_PROCESSING": "false", "OCR_ENABLED": "false", "VLM_ENABLED": "true", "VLM_MODEL": "fake-model",
    "VLM_BASE_URL": "http://127.0.0.1:8081/v1", "OFFLINE_MODE": "true", "RETENTION_DAYS": "0",
})
(TMP / "uploads").mkdir()

import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import app.services.extraction_service as extraction  # noqa: E402
from tests.fake_vlm import FakeVlm, rule_model  # noqa: E402
from tests.test_vlm_pipeline import NANONETS_S04, make_docx  # noqa: E402
import app.api.routes_health as routes_health  # noqa: E402

PORT = 8765
RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


# ---------------------------------------------------------------- seed documents


def seed() -> dict:
    from app.main import create_app

    app = create_app()
    fake = FakeVlm()
    extraction.VlmClient = fake
    routes_health.VlmClient = fake
    docs = {}
    with TestClient(app) as c:
        def up(name, data, ctype):
            r = c.post("/api/v1/documents/upload", files={"file": (name, data, ctype)})
            assert r.status_code == 202, r.text
            return r.json()["document_id"]

        fx = ROOT / "tests" / "fixtures"
        docs["digital"] = up("digital.pdf", (fx / "sample_01_digital_standard.pdf").read_bytes(), "application/pdf")

        fake.fail_map_pages = {2}  # partial: the accused page cannot be mapped
        docs["partial"] = up("multipage.pdf", (fx / "sample_03_multipage.pdf").read_bytes(), "application/pdf")
        fake.fail_map_pages = set()

        def conflicting(body: str) -> dict:  # two pages give different years, both found in their page text
            out = rule_model(body)
            if "CONTINUATION" in body:
                out.setdefault("case", {})["year"] = "99"  # "FSL-99" is printed on page 2
            return out
        fake.answer = conflicting
        docs["conflict"] = up("conflict.pdf", (fx / "sample_01_digital_standard.pdf").read_bytes(), "application/pdf")
        fake.answer = rule_model

        fake.transcripts = {0: NANONETS_S04}
        docs["scanned"] = up("scan.png", _png(), "image/png")
        docs["docx"] = up("report.docx", make_docx(), "application/octet-stream")
        fake.unavailable = True
        docs["down"] = up("down.pdf", (fx / "sample_01_digital_standard.pdf").read_bytes(), "application/pdf")
        fake.unavailable = False
        # the API JSON each page must agree with
        api = {k: c.get(f"/api/v1/documents/{v}/results").json() for k, v in docs.items()}
    return docs, api, app


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- browser (DevTools protocol)


class Browser:
    def __init__(self) -> None:
        exe = os.environ.get("BROWSER_EXE") or next(
            (p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                         shutil.which("msedge") or "", shutil.which("chrome") or "") if p and Path(p).exists()), None)
        if not exe:
            raise SystemExit("No Edge/Chrome found (set BROWSER_EXE).")
        self.proc = subprocess.Popen(
            [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--remote-debugging-port=9333",
             f"--user-data-dir={TMP / 'profile'}", "--window-size=1500,1500", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        from websockets.sync.client import connect

        ws_url = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/list", timeout=2))
                ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.5)
        if not ws_url:
            raise SystemExit("Browser did not start.")
        self.ws = connect(ws_url, max_size=64 * 1024 * 1024)
        self.n = 0
        self.errors: list = []
        self.call("Runtime.enable")
        self.call("Page.enable")

    def call(self, method: str, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=60))
            if msg.get("method") == "Runtime.exceptionThrown":
                self.errors.append(msg["params"]["exceptionDetails"].get("text", "exception"))
            if msg.get("method") == "Runtime.consoleAPICalled" and msg["params"]["type"] == "error":
                self.errors.append("console.error")
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    def js(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"])
        return r["result"].get("value")

    def open(self, url: str, ready: str = "!!document.querySelector('.struct-layout, .failure, .tab')", timeout: int = 30):
        self.call("Page.navigate", url=url)
        self.wait(ready, timeout)

    def wait(self, expr: str, timeout: int = 15):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.js(expr):
                    return
            except RuntimeError:
                pass
            time.sleep(0.25)
        raise TimeoutError(expr)

    def shot(self, path: Path) -> None:
        import base64

        data = self.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)["data"]
        path.write_bytes(base64.b64decode(data))

    def close(self) -> None:
        try:
            self.ws.close()
        finally:
            self.proc.terminate()


# ---------------------------------------------------------------- checks


def shown(v: dict) -> str:
    cv = v.get("corrected_value")
    return str(cv) if cv not in (None, "") else ("" if v.get("value") is None else str(v["value"]))


def expected_rows(structured: dict) -> dict:
    rows = {}
    for sec, fields in structured["single"].items():
        for k, v in fields.items():
            rows[f"{sec}.{k}"] = v
    for sec, recs in structured["lists"].items():
        for i, rec in enumerate(recs):
            for k, v in rec["fields"].items():
                rows[f"{sec}.{i}.{k}"] = v
    return rows


DOM_ROWS = """JSON.stringify([...document.querySelectorAll('#tab-structured tr.s-row[data-path]')].map(tr => {
  const inp = tr.querySelector('.edit-input');
  return {path: tr.dataset.path, fid: inp.dataset.fieldId, value: inp.value, cls: tr.className,
          badge: (tr.querySelector('.badge[class*=st-]')||{}).textContent,
          pages: [...tr.querySelectorAll('a.page-link')].map(a => a.dataset.page),
          cands: tr.querySelectorAll('ul.candidates li').length};
}))"""


def verify_doc(b: Browser, name: str, doc_id: str, body: dict, shots: Path | None) -> None:
    b.open(f"http://127.0.0.1:{PORT}/results/{doc_id}")
    s = body["results"]["structured"]
    layout = body["structured_layout"]
    b.wait("document.querySelectorAll('#tab-structured tr.s-row').length > 0")
    rows = {r["path"]: r for r in json.loads(b.js(DOM_ROWS))}
    want = expected_rows(s)
    check(f"[{name}] every structured value is a row (and no extra rows)", set(rows) == set(want),
          f"missing={sorted(set(want) - set(rows))[:3]} extra={sorted(set(rows) - set(want))[:3]}")
    check(f"[{name}] row values equal the JSON", all(rows[p]["value"] == shown(v) for p, v in want.items() if p in rows))
    check(f"[{name}] status badges equal the JSON",
          all((rows[p]["badge"] or "").replace(" ", "_") == v["status"] for p, v in want.items() if p in rows))
    check(f"[{name}] input ids address the correction API", all(r["fid"] == "s." + r["path"] for r in rows.values()))
    check(f"[{name}] not-found rows are marked", all(("st-not_found" in rows[p]["cls"]) == (v["status"] == "not_found")
                                                    for p, v in want.items() if p in rows))
    check(f"[{name}] conflicting values list every candidate",
          all(rows[p]["cands"] == len(v["candidates"]) for p, v in want.items() if v["status"] == "conflicting" and p in rows))
    check(f"[{name}] review marks match needs_review",
          all(("needs-review" in rows[p]["cls"]) == bool(v["needs_review"] and not v.get("corrected_value"))
              for p, v in want.items() if p in rows))
    for sec in layout:
        if sec["repeated"]:
            n = b.js(f"document.querySelectorAll('#tab-structured details.struct-record[data-record^=\"{sec['key']}.\"]').length")
            check(f"[{name}] {sec['key']}: {len(s['lists'][sec['key']])} record card(s)", n == len(s["lists"][sec["key"]]), f"dom={n}")
    labels = b.js("JSON.stringify([...document.querySelectorAll('#tab-structured tr.s-row .label-cell')].map(e=>e.textContent))")
    expected_labels = [f["label"] for sec in layout if not sec["repeated"] for f in sec["fields"]]
    check(f"[{name}] labels come from the layout", all(l in json.loads(labels) for l in expected_labels))
    check(f"[{name}] Structured is the default tab",
          b.js("document.querySelector('.tab.active').dataset.tab") == "structured"
          and b.js("document.getElementById('tab-fields').classList.contains('hidden')"))
    check(f"[{name}] preview image loads", _wait(b, "!!document.querySelector('.doc-preview img[src]')"))
    if shots:
        b.shot(shots / f"{name}.png")


def _wait(b: Browser, expr: str) -> bool:
    try:
        b.wait(expr, 15)
        return True
    except TimeoutError:
        return False


def interactions(b: Browser, docs: dict, api: dict, shots: Path | None) -> None:
    # --- jump to source page
    b.open(f"http://127.0.0.1:{PORT}/results/{docs['partial']}")
    b.wait("document.querySelectorAll('#tab-structured tr.s-row').length > 0")
    b.js("document.querySelector('.doc-preview select').value")
    b.js("[...document.querySelectorAll('#tab-structured a.page-link')].find(a => a.dataset.page === '3').click()")
    check("clicking a field's page link shows that page",
          _wait(b, "document.querySelector('.doc-preview select').value === '3'"))
    check("preview shows a page image", _wait(b, "document.querySelector('.doc-preview img').naturalWidth > 0"))

    # --- filter
    b.js("(()=>{const f=document.getElementById('struct-filter'); f.value='police'; f.dispatchEvent(new Event('input',{bubbles:true}));})()")
    vis = json.loads(b.js("JSON.stringify([...document.querySelectorAll('#tab-structured tr.s-row:not(.hidden)')].map(t=>t.dataset.path))"))
    check("filter narrows rows to matches", vis == ["case.police_station"], str(vis))
    b.js("(()=>{const f=document.getElementById('struct-filter'); f.value=''; f.dispatchEvent(new Event('input',{bubbles:true}));})()")
    b.js("(()=>{const c=document.getElementById('struct-only-review'); c.checked=true; c.dispatchEvent(new Event('change',{bubbles:true}));})()")
    only = b.js("document.querySelectorAll('#tab-structured tr.s-row:not(.hidden):not(.needs-review)').length")
    check("'only items to review' hides rows that need no review", only == 0, str(only))

    # --- conflict: use a candidate, save, see it as a human correction beside the machine value
    b.open(f"http://127.0.0.1:{PORT}/results/{docs['conflict']}")
    b.wait("document.querySelectorAll('#tab-structured tr.s-row').length > 0")
    n = b.js("document.querySelectorAll('tr[data-path=\"case.year\"] ul.candidates li').length")
    check("conflicting year lists both candidates", n == 2, str(n))
    b.js("document.querySelector('tr[data-path=\"case.year\"] button[data-use-value]').click()")
    check("'use this' fills the value and marks the row edited",
          b.js("document.querySelector('tr[data-path=\"case.year\"]').classList.contains('dirty')"))
    b.js("(()=>{const i=document.querySelector('tr[data-path=\"accused.0.age\"] .edit-input'); i.value='41'; i.dispatchEvent(new Event('input',{bubbles:true}));})()")
    b.js("document.getElementById('save-corrections').click()")
    check("save re-renders with a human-corrected badge",
          _wait(b, "!!document.querySelector('tr[data-path=\"case.year\"] .badge.corrected')"))
    check("view state survives the reload (Structured tab still active)",
          b.js("document.querySelector('.tab.active').dataset.tab") == "structured")
    after = json.loads(b.js(DOM_ROWS))
    row = next(r for r in after if r["path"] == "case.year")
    check("corrected value shown, machine status kept", row["value"] != "" and row["cls"].find("st-conflicting") >= 0)
    import urllib.request as u

    res = json.load(u.urlopen(f"http://127.0.0.1:{PORT}/api/v1/documents/{docs['conflict']}/results"))
    v = res["results"]["structured"]["single"]["case"]["year"]
    check("API keeps the candidates and the correction apart",
          v["value"] is None and v["corrected_value"] == row["value"] and len(v["candidates"]) == 2)
    check("verification still not set by editing", res["document"]["verified"] is False)

    # --- tabs
    b.js("document.querySelector('.tab[data-tab=\"fields\"]').click()")
    check("Fields tab (as-printed) still works",
          b.js("!document.getElementById('tab-fields').classList.contains('hidden') && document.querySelectorAll('tr.pdf-field').length > 0"))
    b.js("document.querySelector('.tab[data-tab=\"raw\"]').click()")
    check("raw_json tab still works", _wait(b, "document.getElementById('raw-body').textContent.includes('\"pages\"')"))
    check("review card hidden on the raw tab", b.js("document.getElementById('review-card').classList.contains('hidden')"))

    # --- failure / partial banners
    b.open(f"http://127.0.0.1:{PORT}/results/{docs['partial']}")
    b.wait("document.querySelectorAll('#tab-structured tr.s-row').length > 0")
    check("partial document shows the partial banner", b.js("document.querySelector('#tab-structured .alert.warn') !== null"))
    check("accused section says none found (not invented)",
          b.js("document.querySelector('#tab-structured details[data-section=\"accused\"]').textContent.includes('none found')"))
    b.open(f"http://127.0.0.1:{PORT}/results/{docs['down']}")
    b.wait("document.querySelectorAll('#tab-structured tr.s-row').length > 0")
    check("unavailable model shows the failed banner and empty values",
          b.js("document.querySelector('#tab-structured .alert.error') !== null")
          and b.js("[...document.querySelectorAll('#tab-structured .edit-input')].every(i => i.value === '')"))
    if shots:
        b.shot(shots / "down.png")
    check("no JavaScript errors in the browser", not b.errors, str(b.errors[:3]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", help="folder for screenshots")
    args = ap.parse_args()
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    docs, api, app = seed()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2)
            break
        except Exception:
            time.sleep(0.25)
    b = Browser()
    try:
        for name in ("digital", "partial", "conflict", "scanned", "docx"):
            verify_doc(b, name, docs[name], api[name], shots)
        interactions(b, docs, api, shots)
    finally:
        b.close()
        server.should_exit = True
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
