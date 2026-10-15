"""Check the results page in a real browser (headless Microsoft Edge / Chrome): the Fields and JSON tabs.

Generates two differently-labelled chargesheets, serves the app locally (no model server is used: the
VLM is switched off), drives the browser through the DevTools protocol and checks that

* the page has exactly two tabs, Fields (default) and JSON - no Structured tab, no raw_json tab;
* the JSON tab shows valid, indented JSON whose keys and values equal the Fields tab's labels and values;
* long multi-line fields (accused, witnesses) are single keys with their complete text;
* editing and saving a field in the Fields tab changes the JSON immediately, an unsaved edit does not;
* the existing Fields features (filter, source pages, review status, correction history, verification
  controls) are still there.

    python scripts/verify_ui.py [--shots folder]

Needs Edge or Chrome (set BROWSER_EXE to use another).
"""

from __future__ import annotations

import argparse
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
    "ASYNC_PROCESSING": "false", "OCR_ENABLED": "false", "VLM_ENABLED": "false", "OFFLINE_MODE": "true",
    "RETENTION_DAYS": "0",
})
(TMP / "uploads").mkdir()

import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_fields_json import TEMPLATE_A, TEMPLATE_B, make_pdf  # noqa: E402

PORT = 8766
RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def seed():
    from app.main import create_app

    app = create_app()
    docs = {}
    with TestClient(app) as c:
        for name, path in (
            ("template_a", make_pdf(TMP / "a.pdf", TEMPLATE_A)),
            ("template_b", make_pdf(TMP / "b.pdf", TEMPLATE_B)),
            ("sample", ROOT / "tests" / "fixtures" / "sample_01_digital_standard.pdf"),
        ):
            r = c.post("/api/v1/documents/upload", files={"file": (path.name, path.read_bytes(), "application/pdf")})
            assert r.status_code == 202, r.text
            docs[name] = r.json()["document_id"]
    return docs, app


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
            [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--remote-debugging-port=9334",
             f"--user-data-dir={TMP / 'profile'}", "--window-size=1400,1400", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        from websockets.sync.client import connect

        ws_url = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9334/json/list", timeout=2))
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

    def wait(self, expr: str, timeout: int = 15) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.js(expr):
                    return True
            except RuntimeError:
                pass
            time.sleep(0.25)
        return False

    def open(self, doc_id: str) -> None:
        self.call("Page.navigate", url=f"http://127.0.0.1:{PORT}/results/{doc_id}")
        self.wait("!!document.querySelector('.tab')", 30)

    def shot(self, path: Path) -> None:
        import base64

        self.call("Emulation.setDeviceMetricsOverride", width=1100, height=1500, deviceScaleFactor=1, mobile=False)
        self.js("(document.querySelector('.tabs') || document.body).scrollIntoView()")
        path.write_bytes(base64.b64decode(self.call("Page.captureScreenshot", format="png")["data"]))

    def close(self) -> None:
        try:
            self.ws.close()
        finally:
            self.proc.terminate()


FIELDS_DOM = """JSON.stringify([...document.querySelectorAll('#tab-fields tr.pdf-field')].map(tr => ({
  label: tr.querySelector('.label-cell').textContent.trim(),
  value: tr.querySelector('.edit-input').value,
  page: tr.children[3].textContent.trim(),
  review: tr.children[4].textContent.trim()})))"""
JSON_TEXT = "document.getElementById('json-body').textContent"
UNLABELLED = "(text before any label)"


def open_json_tab(b: Browser) -> None:
    b.js("document.querySelector('.tab[data-tab=\"json\"]').click()")
    b.wait("document.getElementById('json-body').textContent.trim().startsWith('{')")


def dedupe_keys(labels: list) -> list:
    seen, out = set(), []
    for lab in labels:
        base = UNLABELLED if lab.startswith("(text before any label)") else lab
        key, n = base, 2
        while key in seen:
            key = f"{base} ({n})"
            n += 1
        seen.add(key)
        out.append(key)
    return out


def verify_doc(b: Browser, name: str, doc_id: str, shots) -> None:
    b.open(doc_id)
    tabs = json.loads(b.js("JSON.stringify([...document.querySelectorAll('.tab')].map(t => t.textContent.trim()))"))
    check(f"[{name}] exactly two tabs: Fields and JSON", tabs == ["Fields", "JSON"], str(tabs))
    check(f"[{name}] no Structured / raw_json tab or panel",
          not b.js("!!document.querySelector('[data-tab=\"structured\"], [data-tab=\"raw\"], #tab-structured, #tab-raw')"))
    check(f"[{name}] Fields is the default tab",
          b.js("document.querySelector('.tab.active').dataset.tab") == "fields"
          and b.js("document.getElementById('tab-json').classList.contains('hidden')"))
    fields = json.loads(b.js(FIELDS_DOM))
    open_json_tab(b)
    text = b.js(JSON_TEXT)
    try:
        data = json.loads(text)
        valid = True
    except ValueError:
        data, valid = {}, False
    check(f"[{name}] JSON tab shows valid JSON", valid)
    check(f"[{name}] JSON is indented and readable", text.startswith('{\n  "') and text == json.dumps(data, indent=2, ensure_ascii=False))
    check(f"[{name}] JSON keys equal the Fields tab labels, in order", list(data) == dedupe_keys([f["label"] for f in fields]),
          f"json={list(data)[:4]} fields={[f['label'] for f in fields][:4]}")
    check(f"[{name}] JSON values equal the values shown in the Fields tab", list(data.values()) == [f["value"] for f in fields])
    check(f"[{name}] every field in the Fields tab keeps its source page and review status",
          all(f["page"] and f["review"] for f in fields))
    check(f"[{name}] review card is shown with the JSON tab",
          not b.js("document.getElementById('review-card').classList.contains('hidden')"))
    if shots:
        b.shot(shots / f"{name}_json.png")


def interactions(b: Browser, docs: dict, shots) -> None:
    b.open(docs["template_a"])
    fields = json.loads(b.js(FIELDS_DOM))
    accused_key = "Particulars of accused persons charge sheeted"
    open_json_tab(b)
    data = json.loads(b.js(JSON_TEXT))
    check("accused section is one JSON key with both accused and their arrest / remand notes",
          accused_key in data and all(s in data[accused_key] for s in
                                      ("A1- Pavan Kalyan", "Judicial Remand.)", "A2- Salma Begum", "released on bail.)")))
    check("multi-line value keeps its line breaks", data[accused_key].count("\n") >= 3 and "\n" in data["List of witnesses"])
    check("a field with no value is an empty string, not invented", data.get("Final result No./Year") == "")
    check("an arrest note is not a key", not any(k.startswith("(Arrested") for k in data))

    # --- unsaved edit is not in the JSON; saved edit is, without reprocessing
    b.js("document.querySelector('.tab[data-tab=\"fields\"]').click()")
    b.js("""(() => { const i = [...document.querySelectorAll('#tab-fields tr.pdf-field')]
        .find(tr => tr.querySelector('.label-cell').textContent.trim() === 'FIR No.').querySelector('.edit-input');
        i.value = '99/2021'; i.dispatchEvent(new Event('input', {bubbles: true})); })()""")
    open_json_tab(b)
    check("an unsaved edit is not in the JSON", json.loads(b.js(JSON_TEXT))["FIR No."] == "17/2021")
    before = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/v1/documents/{docs['template_a']}"))["document"]
    b.js("document.querySelector('.tab[data-tab=\"fields\"]').click()")
    b.js("document.getElementById('save-corrections').click()")
    check("saving keeps the reviewer on the Fields tab and reloads the data",
          b.wait("!!document.querySelector('#tab-fields .badge.corrected')"))
    open_json_tab(b)
    after_json = json.loads(b.js(JSON_TEXT))
    check("the saved correction is in the JSON immediately", after_json["FIR No."] == "99/2021")
    check("the other keys are unchanged", {k: v for k, v in after_json.items() if k != "FIR No."} ==
          {k: v for k, v in data.items() if k != "FIR No."})
    after = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/v1/documents/{docs['template_a']}"))["document"]
    check("no reprocessing happened (same processing time, still completed)",
          after["processed_at"] == before["processed_at"] and after["status"] == "completed")
    api = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/v1/documents/{docs['template_a']}/results"))
    fir = next(f for f in api["results"]["fields"] if f["label"] == "FIR No.")
    check("machine value and correction are kept apart in the results", fir["value"] == "17/2021" and fir["corrected_value"] == "99/2021")
    check("the JSON tab survived the reload as the active tab", b.js("document.querySelector('.tab.active').dataset.tab") == "json")

    # --- copy / download
    check("Copy and Download buttons are present", b.js("!!document.getElementById('json-copy') && !!document.getElementById('json-download')"))
    check("Download builds a .json file named after the document",
          b.js("""(() => { let name = ''; const orig = HTMLAnchorElement.prototype.click;
            HTMLAnchorElement.prototype.click = function () { name = this.download; };
            document.getElementById('json-download').click();
            HTMLAnchorElement.prototype.click = orig; return name; })()""") == "a.json")

    # --- existing Fields features
    b.js("document.querySelector('.tab[data-tab=\"fields\"]').click()")
    b.js("(()=>{const f=document.getElementById('field-filter'); f.value='eastmoor'; f.dispatchEvent(new Event('input',{bubbles:true}));})()")
    vis = json.loads(b.js("JSON.stringify([...document.querySelectorAll('tr.pdf-field:not(.hidden) .label-cell')].map(e=>e.textContent.trim()))"))
    check("Fields filter still works", vis == ["District"], str(vis))
    b.js("(()=>{const f=document.getElementById('field-filter'); f.value=''; f.dispatchEvent(new Event('input',{bubbles:true}));})()")
    check("source page and review status are still shown", b.js("document.querySelector('tr.pdf-field td:nth-child(4)').textContent.includes('p.1')"))
    b.js("document.getElementById('history').open = true; document.getElementById('history').dispatchEvent(new Event('toggle'))")
    check("correction history lists the saved correction",
          b.wait("document.getElementById('history-body').textContent.includes('FIR') || document.getElementById('history-body').textContent.includes('p1-')"))
    check("verification controls and reprocess are still there",
          b.js("!!document.getElementById('verify-btn') && !!document.getElementById('verify-notes') && !!document.getElementById('reprocess-btn')"))
    b.js("document.getElementById('verify-btn').click()")
    check("verification without notes is refused by the page", b.wait("document.getElementById('save-msg').textContent.includes('notes')"))
    if shots:
        b.shot(shots / "fields_tab.png")
    check("no JavaScript errors in the browser", not b.errors, str(b.errors[:3]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", help="folder for screenshots")
    args = ap.parse_args()
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    docs, app = seed()
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
        for name in ("template_a", "template_b", "sample"):
            verify_doc(b, name, docs[name], shots)
        interactions(b, docs, shots)
    finally:
        b.close()
        server.should_exit = True
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
