/* Upload page of the unified application: one upload for every supported file type, automatic or manual document type.
 * Talks only to the unified upload / intake API. No inline script (the pages' CSP allows scripts from this origin only).
 */
(function () {
  "use strict";
  const body = document.body;
  if ((body.dataset.page || "") !== "unified-upload") return;

  const KEY_STORAGE = "ir_api_key";               // the key box on this page shares the host UI's storage name
  const HEADER = body.dataset.apiKeyHeader || "X-API-Key";
  const ACCEPT = [".pdf", ".docx", ".png", ".jpg", ".jpeg", ".tif", ".tiff"];
  const LABEL = { charge_sheet: "Charge sheet", interrogation_report: "Interrogation report", unknown: "Unknown" };

  const $ = (id) => document.getElementById(id);
  const zone = $("u-dropzone"), input = $("u-file-input"), btn = $("u-upload-btn"), typeSel = $("u-type");
  const statusEl = $("u-status"), progress = $("u-progress"), bar = progress.querySelector("span");
  const chooseCard = $("u-choose"), chooseWhy = $("u-choose-why");
  let selected = null;
  let waiting = null;                              // intake currently shown in the "choose" card

  function key() { try { return sessionStorage.getItem(KEY_STORAGE) || ""; } catch (e) { return ""; } }
  function headers(extra) { const h = { ...(extra || {}) }; const k = key(); if (k) h[HEADER] = k; return h; }
  function esc(s) {
    return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function size(n) { return n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(2)} MB`; }
  function alertBox(el, text, cls) { el.className = `alert ${cls || "info"}`; el.textContent = text; }
  function detail(b, fallback) {
    if (!b) return fallback;
    const d = b.detail !== undefined ? b.detail : b.error;
    if (typeof d === "string") return d;
    if (d && d.message) return d.message;
    return typeof b === "string" ? b : fallback;
  }

  /* API key box (the host script only wires it on its own pages) */
  const keyInput = $("api-key-input"), keySave = $("api-key-save"), keyMsg = $("api-key-msg");
  if (keyInput) {
    keyInput.value = key();
    keySave.addEventListener("click", () => {
      try { sessionStorage.setItem(KEY_STORAGE, keyInput.value.trim()); } catch (e) { /* storage blocked */ }
      alertBox(keyMsg, keyInput.value.trim() ? "Key saved for this browser tab." : "Key cleared.", "ok");
      loadPending();
    });
    fetch("/health").then((r) => r.json()).then((h) => {
      const box = $("api-key-box");
      if (box && !h.auth_enabled) box.classList.add("hidden");
    }).catch(() => {});
  }
  const nav = document.querySelector('nav a[href="/"]');
  if (nav) nav.classList.add("active");

  function supported(name) { const n = name.toLowerCase(); return ACCEPT.some((e) => n.endsWith(e)); }

  function showFile(file) {
    selected = file;
    $("u-file-meta").innerHTML = `<div class="meta-grid">
      <div class="meta-item"><div class="label">Name</div><div class="value">${esc(file.name)}</div></div>
      <div class="meta-item"><div class="label">Size</div><div class="value">${size(file.size)}</div></div></div>`;
    btn.disabled = false;
  }
  zone.addEventListener("click", () => input.click());
  zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") input.click(); });
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("dragover"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
  zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("dragover"); if (e.dataTransfer.files[0]) showFile(e.dataTransfer.files[0]); });
  input.addEventListener("change", () => { if (input.files[0]) showFile(input.files[0]); });

  function openResults(id) { window.location.href = `/results/${encodeURIComponent(id)}`; }

  function showChoice(res) {
    waiting = res;
    const c = res.classification || {};
    const reasons = (c.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("");
    const cand = (res.candidates || c.candidates || []).map((x) => `${esc(LABEL[x.document_type] || x.document_type)} ${Number(x.score).toFixed(2)}`).join(" · ");
    const warn = (c.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");
    chooseWhy.innerHTML = `<div class="alert warn">The document type could not be decided with confidence, so it has <strong>not</strong> been sent to any extractor.<ul>${reasons}</ul>${cand ? `<div class="small">Scores: ${cand}</div>` : ""}${warn ? `<ul>${warn}</ul>` : ""}</div>`;
    chooseCard.classList.remove("hidden");
    chooseCard.scrollIntoView({ block: "nearest" });
  }

  async function route(intakeId, type) {
    const res = await fetch(`/api/v1/unified/intakes/${encodeURIComponent(intakeId)}/route`, {
      method: "POST", headers: headers({ "Content-Type": "application/json" }), body: JSON.stringify({ document_type: type }),
    });
    const b = await res.json().catch(() => null);
    if (res.status === 202 && b && b.document_id) return openResults(b.document_id);
    throw new Error(detail(b, `HTTP ${res.status}`));
  }

  chooseCard.querySelectorAll("button[data-type]").forEach((b) => b.addEventListener("click", async () => {
    if (!waiting) return;
    try { await route(waiting.intake_id, b.dataset.type); } catch (e) { alertBox(statusEl, e.message, "error"); }
  }));
  $("u-pick-discard").addEventListener("click", async () => {
    if (!waiting) return;
    await fetch(`/api/v1/unified/intakes/${encodeURIComponent(waiting.intake_id)}`, { method: "DELETE", headers: headers() });
    waiting = null; chooseCard.classList.add("hidden"); alertBox(statusEl, "Upload discarded.", "info"); loadPending();
  });

  btn.addEventListener("click", () => {
    if (!selected) return;
    if (!supported(selected.name)) { alertBox(statusEl, `Accepted file types: ${ACCEPT.join(", ")}`, "error"); return; }
    btn.disabled = true; chooseCard.classList.add("hidden");
    progress.style.display = "block"; bar.style.width = "0%";
    alertBox(statusEl, "Uploading…", "info");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/v1/documents/upload");
    const k = key(); if (k) xhr.setRequestHeader(HEADER, k);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.style.width = `${Math.round((e.loaded / e.total) * 100)}%`; };
    xhr.onload = () => {
      let b = null; try { b = JSON.parse(xhr.responseText); } catch (e) { b = xhr.responseText; }
      if (xhr.status === 202 && b && b.status === "awaiting_type") {
        bar.style.width = "100%"; alertBox(statusEl, "Uploaded. Choose the document type to continue.", "info"); showChoice(b); loadPending();
      } else if (xhr.status === 202 && b && b.document_id) {
        bar.style.width = "100%";
        const how = b.document_type_source === "manual" ? "selected" : "detected";
        alertBox(statusEl, `Uploaded as ${LABEL[b.document_type] || b.document_type} (${how}). Opening results — processing continues on the server…`, "ok");
        openResults(b.document_id);
      } else {
        alertBox(statusEl, `Upload failed (${xhr.status}): ${detail(b, "unknown error")}`, "error");
        btn.disabled = false; bar.style.width = "0%";
      }
    };
    xhr.onerror = () => { alertBox(statusEl, "Upload failed: the server could not be reached.", "error"); btn.disabled = false; };
    const fd = new FormData();
    fd.append("file", selected);
    fd.append("document_type", typeSel.value);
    xhr.send(fd);
  });

  async function loadPending() {
    const host = $("u-pending-list"), card = $("u-pending");
    try {
      const res = await fetch("/api/v1/unified/intakes", { headers: headers() });
      if (!res.ok) return;
      const b = await res.json();
      const items = (b.items || []).filter((i) => !waiting || i.intake_id !== waiting.intake_id);
      card.classList.toggle("hidden", items.length === 0);
      host.innerHTML = items.map((i) => `<div class="row" data-id="${esc(i.intake_id)}" style="margin:0.4rem 0">
          <span>${esc(i.original_filename)}</span><span class="muted small">${esc(i.created_at)}</span>
          <button class="btn secondary" data-type="charge_sheet">Charge sheet</button>
          <button class="btn secondary" data-type="interrogation_report">Interrogation report</button>
          <button class="btn secondary" data-discard="1">Discard</button></div>`).join("");
      host.querySelectorAll(".row").forEach((row) => {
        row.querySelectorAll("button[data-type]").forEach((b2) => b2.addEventListener("click", async () => {
          try { await route(row.dataset.id, b2.dataset.type); } catch (e) { alertBox(statusEl, e.message, "error"); }
        }));
        row.querySelector("[data-discard]").addEventListener("click", async () => {
          await fetch(`/api/v1/unified/intakes/${encodeURIComponent(row.dataset.id)}`, { method: "DELETE", headers: headers() });
          loadPending();
        });
      });
    } catch (e) { /* the list is a convenience */ }
  }
  loadPending();
})();
