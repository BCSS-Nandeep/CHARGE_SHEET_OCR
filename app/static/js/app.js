/* Charge Sheet UI — talks to the local REST API. */

const API_KEY_STORAGE = "cs_api_key";
const API_KEY_HEADER = (document.body && document.body.dataset.apiKeyHeader) || "X-API-Key";

/* ---------- helpers ---------- */

function getApiKey() {
  // sessionStorage: the key is forgotten when the tab closes
  try { return sessionStorage.getItem(API_KEY_STORAGE) || ""; } catch (e) { return ""; }
}

function setApiKey(value) {
  try { sessionStorage.setItem(API_KEY_STORAGE, value || ""); } catch (e) { /* storage blocked */ }
}

function authHeaders(extra = {}) {
  const headers = { ...extra };
  const key = getApiKey();
  if (key) headers[API_KEY_HEADER] = key;
  return headers;
}

function errorText(body, fallback) {
  if (!body) return fallback;
  const d = body.detail !== undefined ? body.detail : body.error;
  if (typeof d === "string") return d;
  if (d && typeof d === "object" && d.message) return d.message;
  if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join("; ");
  return typeof body === "string" ? body : fallback;
}

async function api(path, options = {}) {
  const opts = { ...options };
  opts.headers = authHeaders(opts.headers || {});
  const res = await fetch(path, opts);
  const ct = res.headers.get("content-type") || "";
  const body = ct.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) {
    const err = new Error(errorText(body, res.statusText));
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function formatElapsed(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}

function badge(status, title) {
  const s = String(status || "unknown").toLowerCase().replace(/[^a-z_]/g, "");
  return `<span class="badge ${s}"${title ? ` title="${escapeHtml(title)}"` : ""}>${escapeHtml(s.replace(/_/g, " "))}</span>`;
}

function shownValue(f) {
  if (!f) return "";
  if (f.corrected_value != null && f.corrected_value !== "") return String(f.corrected_value);
  return f.value == null ? "" : String(f.value);
}

function setAlert(el, text, cls) {
  if (!el) return;
  el.className = `alert ${cls || "info"}`;
  el.textContent = text;
  el.classList.remove("hidden");
}

/* ---------- API key box (all pages) ---------- */

function bindApiKeyForm() {
  const input = document.getElementById("api-key-input");
  const saveBtn = document.getElementById("api-key-save");
  const msg = document.getElementById("api-key-msg");
  if (!input) return;
  input.value = getApiKey();
  saveBtn.addEventListener("click", () => {
    setApiKey(input.value.trim());
    setAlert(msg, input.value.trim() ? "Key saved for this browser tab." : "Key cleared.", "ok");
  });
  fetch("/health").then((r) => r.json()).then((h) => {
    const box = document.getElementById("api-key-box");
    const status = document.getElementById("service-status");
    if (box && !h.auth_enabled) box.classList.add("hidden");
    if (status) {
      const parts = [
        h.auth_enabled ? "authentication on" : "authentication off (local only, verification disabled)",
        h.ocr_available ? `OCR: ${h.ocr_engine}` : "OCR unavailable — scanned PDFs will fail",
        h.ppstructure_available ? "PP-Structure on" : "PP-Structure off",
        h.docling_available ? "Docling on" : "Docling off",
        h.vl_available ? "VL on" : "VL off",
        h.vlm_enabled ? (h.vlm_available ? `vision model: ${h.vlm_model}` : `vision model unavailable${h.vlm_error ? " (" + h.vlm_error + ")" : ""}`) : "vision model off",
        h.offline_mode ? "offline mode" : "network allowed",
      ];
      status.textContent = parts.join(" · ");
      if (!h.ocr_available || h.auth_misconfigured || (h.vlm_enabled && !h.vlm_available)) status.classList.add("warn-text");
    }
  }).catch(() => {});
}

/* ---------- Upload page ---------- */

function initUploadPage() {
  bindApiKeyForm();
  const zone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const uploadBtn = document.getElementById("upload-btn");
  const statusEl = document.getElementById("upload-status");
  const progress = document.getElementById("progress");
  const bar = progress.querySelector("span");
  let selected = null;

  function showFile(file) {
    selected = file;
    document.getElementById("file-meta").innerHTML = `
      <div class="meta-grid">
        <div class="meta-item"><div class="label">Name</div><div class="value">${escapeHtml(file.name)}</div></div>
        <div class="meta-item"><div class="label">Size</div><div class="value">${formatBytes(file.size)}</div></div>
      </div>`;
    uploadBtn.disabled = false;
  }

  zone.addEventListener("click", () => fileInput.click());
  zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") fileInput.click(); });
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("dragover"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("dragover");
    if (e.dataTransfer.files[0]) showFile(e.dataTransfer.files[0]);
  });
  fileInput.addEventListener("change", () => { if (fileInput.files[0]) showFile(fileInput.files[0]); });

  uploadBtn.addEventListener("click", () => {
    if (!selected) return;
    if (!/\.(pdf|png|jpe?g|tiff?|docx)$/i.test(selected.name)) {
      setAlert(statusEl, "Accepted files: PDF, PNG, JPEG, TIFF or DOCX.", "error");
      return;
    }
    uploadBtn.disabled = true;
    progress.style.display = "block";
    bar.style.width = "0%";
    setAlert(statusEl, "Uploading…", "info");

    // XHR gives real upload progress events (fetch does not)
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/v1/documents/upload");
    const key = getApiKey();
    if (key) xhr.setRequestHeader(API_KEY_HEADER, key);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) bar.style.width = `${Math.round((e.loaded / e.total) * 100)}%`;
    };
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch (e) { body = xhr.responseText; }
      if (xhr.status === 202 && body && body.document_id) {
        bar.style.width = "100%";
        setAlert(statusEl, "Uploaded. Opening results — processing continues on the server…", "ok");
        window.location.href = `/results/${encodeURIComponent(body.document_id)}`;
      } else {
        setAlert(statusEl, `Upload failed (${xhr.status}): ${errorText(body, "unknown error")}`, "error");
        uploadBtn.disabled = false;
        bar.style.width = "0%";
      }
    };
    xhr.onerror = () => {
      setAlert(statusEl, "Upload failed: the server could not be reached.", "error");
      uploadBtn.disabled = false;
    };
    const fd = new FormData();
    fd.append("file", selected);
    xhr.send(fd);
  });
}

/* ---------- Results page ---------- */

function initResultsPage(documentId) {
  bindApiKeyForm();
  const root = document.getElementById("results-root");
  const viewState = { tab: null };  // survives the re-render after saving
  const pollNote = document.getElementById("poll-note");
  const docUrl = `/api/v1/documents/${encodeURIComponent(documentId)}`;

  async function load() {
    let doc;
    try {
      doc = (await api(docUrl)).document;
    } catch (err) {
      root.innerHTML = `<div class="alert error">${escapeHtml(err.message)}${err.status === 401 ? " — enter an API key above." : ""}</div>`;
      return;
    }
    renderMeta(doc);
    if (["queued", "processing", "uploaded"].includes(doc.status)) {
      pollNote.classList.remove("hidden");
      const p = doc.progress;
      pollNote.textContent = p
        ? `Processing — ${p.step} · ${formatElapsed(p.elapsed_seconds)} elapsed. Pages are read locally: a scanned page can take from half a minute (OCR) to several minutes (vision model). This page refreshes automatically.`
        : `Waiting to start (status: ${doc.status}). This page refreshes automatically.`;
      root.innerHTML = "";
      setTimeout(load, 2500);
      return;
    }
    pollNote.classList.add("hidden");
    if (doc.status === "failed") {
      renderFailure(doc);
      return;
    }
    try {
      renderResults(doc, await api(`${docUrl}/results`));
    } catch (err) {
      root.innerHTML = `<div class="alert error">${escapeHtml(err.message)}</div>`;
    }
  }

  function renderMeta(doc) {
    document.getElementById("doc-meta").innerHTML = `
      <div class="meta-grid">
        <div class="meta-item"><div class="label">Filename</div><div class="value">${escapeHtml(doc.original_filename)}</div></div>
        <div class="meta-item"><div class="label">Status</div><div class="value">${badge(doc.status)}</div></div>
        <div class="meta-item"><div class="label">Pages</div><div class="value">${escapeHtml(doc.page_count ?? "—")}</div></div>
        <div class="meta-item"><div class="label">Scanned (OCR)</div><div class="value">${doc.is_scanned == null ? "—" : (doc.is_scanned ? "yes" : "no")}</div></div>
        <div class="meta-item"><div class="label">Verified</div><div class="value">${doc.verified ? `yes — ${escapeHtml(doc.verified_by || "")}` : "no"}</div></div>
        <div class="meta-item"><div class="label">Uploaded</div><div class="value">${escapeHtml(doc.created_at)}</div></div>
        <div class="meta-item"><div class="label">Document ID</div><div class="value mono">${escapeHtml(doc.id)}</div></div>
        <div class="meta-item"><div class="label">Original file</div><div class="value"><a href="#" id="source-link">Open original</a></div></div>
      </div>`;
    document.getElementById("doc-meta").dataset.filename = doc.original_filename || "";
    document.getElementById("source-link").addEventListener("click", async (e) => {
      e.preventDefault();
      try {
        const res = await fetch(`${docUrl}/source`, { headers: authHeaders() });
        if (!res.ok) throw new Error(`Unable to open source PDF (${res.status})`);
        const url = URL.createObjectURL(await res.blob());
        window.open(url, "_blank", "noopener");
      } catch (err) {
        alert(err.message);
      }
    });
  }

  async function renderFailure(doc) {
    let diag = null;
    try {
      await api(`${docUrl}/results`);
    } catch (err) {
      diag = err.body && typeof err.body.detail === "object" ? err.body.detail : null;
    }
    root.innerHTML = `
      <div class="card section failure">
        <h2>Extraction failed — no results were produced</h2>
        <p>${escapeHtml(doc.error_message || "Processing failed.")}</p>
        ${diag ? stagesTable(diag.stage_results) + pagesTable(diag.page_summaries) + warningsList(diag.warnings) : ""}
        <div class="row"><button class="btn" id="retry-btn">Reprocess</button><span id="retry-msg" class="hidden"></span></div>
      </div>`;
    document.getElementById("retry-btn").onclick = () => reprocess(document.getElementById("retry-msg"));
  }

  async function reprocess(msgEl) {
    try {
      await api(`${docUrl}/reprocess`, { method: "POST" });
      load();
    } catch (err) {
      setAlert(msgEl, err.message, "error");
    }
  }

  function stagesTable(stages) {
    if (!stages || !stages.length) return "";
    const rows = stages.map((s) => `<tr><td>${escapeHtml(s.stage.replace(/_/g, " "))}</td><td>${badge(s.status)}</td><td>${escapeHtml(s.detail || "")}</td></tr>`).join("");
    return `<h3>Pipeline stages</h3><table class="field-table"><thead><tr><th>Stage</th><th>Status</th><th>Detail</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function pagesTable(pages) {
    if (!pages || !pages.length) return "";
    const rows = pages.map((p) => `<tr><td>${p.page_number}</td>
      <td>${badge(p.route || p.text_source || "—")}</td>
      <td>${badge(p.page_status || (p.text_source === "none" ? "failed" : "ok"))}</td>
      <td>${badge(p.text_source === "none" ? "missing" : p.text_source)}</td>
      <td>${p.char_count}</td><td>${p.ocr_confidence == null ? "—" : escapeHtml(p.ocr_confidence)}</td>
      <td>${escapeHtml(p.note || "")}</td></tr>`).join("");
    return `<h3>Pages</h3><table class="field-table"><thead><tr><th>Page</th><th>Route</th><th>Status</th><th>Text from</th><th>Characters</th><th>OCR confidence</th><th>Note</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function unresolvedSection(report) {
    if (!report || !(report.items || []).length) return "";
    const items = report.items.map((it) => `<li><strong>${escapeHtml(it.code)}</strong>${it.page != null ? ` (p.${escapeHtml(it.page)})` : ""}
      ${badge(it.severity || "review")}: ${escapeHtml(it.detail || "")}</li>`).join("");
    return `<div class="card section"><h3>Unresolved content (${report.items.length})${report.needs_review ? " — needs review" : ""}</h3>
      <p class="muted">Gaps the pipeline did not invent values for. Confirm against the source PDF.</p>
      <ul class="conflicts">${items}</ul></div>`;
  }

  function warningsList(warnings) {
    if (!warnings || !warnings.length) return "";
    return `<ul class="warnings">${warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul>`;
  }

  /* One field as printed in the PDF: its own label and its value (editable) */
  function pdfFieldRow(f) {
    const value = shownValue(f);
    const corrected = f.corrected_value != null && f.corrected_value !== "";
    const multi = value.length > 90 || value.includes("\n");
    const lines = Math.min(28, Math.max(3, Math.ceil(value.length / 95) + (value.match(/\n/g) || []).length));
    const input = multi
      ? `<textarea class="edit-input value-input" rows="${lines}" data-field-id="${escapeHtml(f.id)}" data-initial="${escapeHtml(value)}">${escapeHtml(value)}</textarea>`
      : `<input type="text" class="edit-input value-input" data-field-id="${escapeHtml(f.id)}" data-initial="${escapeHtml(value)}" value="${escapeHtml(value)}" />`;
    const pages = f.pages && f.pages.length > 1 ? `p.${f.pages[0]}–${f.pages[f.pages.length - 1]}` : `p.${f.page}`;
    const label = f.label
      ? escapeHtml(f.label)
      : `<span class="muted"><em>(text before any label)</em></span>`;
    const original = corrected
      ? `<details><summary class="muted small">extracted value (before correction)</summary><div class="source-snippet">${escapeHtml(f.value)}</div></details>` : "";
    return `<tr class="pdf-field" data-search="${escapeHtml(((f.label || "") + " " + value).toLowerCase())}">
      <td class="serial-cell muted">${escapeHtml(f.serial || "")}</td>
      <td class="label-cell">${label}</td>
      <td>${input}${original}${f.notes ? `<div class="muted small">${escapeHtml(f.notes)}</div>` : ""}</td>
      <td>${escapeHtml(pages)}${f.confidence != null ? `<div class="muted small" title="OCR confidence, not accuracy">OCR ${escapeHtml(f.confidence)}</div>` : ""}</td>
      <td>${badge(corrected ? "corrected" : f.review_status)}</td>
    </tr>`;
  }

  /* Fields in reading order, one table per page where they start */
  function pageSections(fields) {
    if (!fields.length) {
      return `<div class="card section"><p class="muted">No labels or values were detected in this PDF (nothing was invented).</p></div>`;
    }
    const byPage = new Map();
    fields.forEach((f) => {
      if (!byPage.has(f.page)) byPage.set(f.page, []);
      byPage.get(f.page).push(f);
    });
    return [...byPage.entries()].map(([page, list]) => `
      <div class="card section page-section">
        <h3>Page ${escapeHtml(page)} <span class="muted small">(${list.length} field${list.length === 1 ? "" : "s"})</span></h3>
        <table class="field-table pdf-fields"><thead><tr><th>No.</th><th>Label (as in the PDF)</th><th>Value (as in the PDF)</th><th>Page</th><th>Review</th></tr></thead>
        <tbody>${list.map(pdfFieldRow).join("")}</tbody></table>
      </div>`).join("");
  }

  /* JSON tab: the final JSON, built by the server from the same fields as the Fields tab.
     Keys are the labels detected in the document, values the complete final text (corrections included). */
  function bindTabs() {
    let text = "";
    const body = document.getElementById("json-body");
    const msg = document.getElementById("json-msg");

    async function loadJson() {
      body.textContent = "Loading…";
      try {
        const data = await api(`${docUrl}/json`);
        text = JSON.stringify(data, null, 2);
        body.textContent = text;
        document.getElementById("json-count").textContent = `${Object.keys(data).length} key(s)`;
      } catch (err) {
        text = "";
        body.textContent = err.message;
      }
    }

    root.querySelectorAll(".tab").forEach((btn) => {
      btn.onclick = () => {
        const tab = btn.dataset.tab;
        viewState.tab = tab;
        root.querySelectorAll(".tab").forEach((b) => {
          b.classList.toggle("active", b === btn);
          b.setAttribute("aria-selected", String(b === btn));
        });
        root.querySelectorAll('[id^="tab-"]').forEach((panel) => panel.classList.toggle("hidden", panel.id !== `tab-${tab}`));
        if (tab === "json") loadJson();  // always fresh: a saved correction shows up without reprocessing
      };
    });
    document.getElementById("json-copy").onclick = async () => {
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        setAlert(msg, "JSON copied.", "ok");
      } catch (err) {
        setAlert(msg, "Copy was blocked by the browser; use Download instead.", "error");
      }
    };
    document.getElementById("json-download").onclick = () => {
      if (!text) return;
      const name = (document.getElementById("doc-meta").dataset.filename || "chargesheet").replace(/\.[A-Za-z0-9]+$/, "").replace(/[^A-Za-z0-9._-]+/g, "_");
      const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = `${name}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    };
    if (viewState.tab === "json") loadJson();
  }

  function renderResults(doc, payload) {
    const r = payload.results;
    if (!r) {
      root.innerHTML = `<div class="alert info">No results yet.</div>`;
      return;
    }
    const fields = r.fields || [];
    // The reviewer's tab survives the reload that follows saving a correction
    const tabs = [["fields", "Fields"], ["json", "JSON"]];
    const firstTab = tabs.some(([key]) => key === viewState.tab) ? viewState.tab : "fields";
    viewState.tab = firstTab;
    const legacy = (r.extraction_warnings || []).some((w) => w.startsWith("REPROCESS_REQUIRED"));
    const banner = legacy
      ? `<div class="alert warn"><strong>Reprocess required.</strong> This result was made by an older version with a fixed field list. Click <em>Reprocess</em> to detect the fields in this PDF.</div>`
      : r.extraction_status === "partial"
        ? `<div class="alert warn"><strong>Partial extraction.</strong> Some pages could not be read fully; their fields may be missing. See pipeline stages and pages below.</div>`
        : "";
    const labelled = fields.filter((f) => f.label).length;

    root.innerHTML = `
      <div class="card">
        <h2>Extraction summary</h2>
        ${banner}
        <div class="meta-grid">
          <div class="meta-item"><div class="label">Extraction</div><div class="value">${badge(r.extraction_status)}</div></div>
          <div class="meta-item"><div class="label">Document type</div><div class="value">${escapeHtml(r.document_info?.document_type || "—")}</div></div>
          <div class="meta-item"><div class="label">Fields found</div><div class="value">${fields.length} (${labelled} with a label)</div></div>
          <div class="meta-item"><div class="label">OCR confidence (not accuracy)</div><div class="value">${escapeHtml(r.overall_confidence ?? "—")}</div></div>
          <div class="meta-item"><div class="label">Schema</div><div class="value">${escapeHtml(r.schema_version)}</div></div>
        </div>
        ${warningsList(r.extraction_warnings)}
        <details><summary>Pipeline stages and pages</summary>${stagesTable(r.stage_results)}${pagesTable(r.page_summaries)}</details>
      </div>
      <div class="tabs" role="tablist">
        ${tabs.map(([key, text]) => `<button class="tab${key === firstTab ? " active" : ""}" role="tab" data-tab="${key}" aria-selected="${key === firstTab}">${text}</button>`).join("")}
      </div>
      <div id="tab-json"${firstTab === "json" ? "" : ' class="hidden"'}>
        <div class="card section">
          <div class="row">
            <button class="btn secondary" id="json-copy">Copy</button>
            <button class="btn secondary" id="json-download">Download JSON</button>
            <span class="muted small" id="json-count"></span>
            <span class="muted small">One key per field, named as printed in this document; values are the complete final text, including saved corrections. Unsaved edits are not included.</span>
          </div>
          <div id="json-msg" class="hidden"></div>
          <pre id="json-body" class="raw-json json-view">Loading…</pre>
        </div>
      </div>
      <div id="tab-fields"${firstTab === "fields" ? "" : ' class="hidden"'}>
      ${unresolvedSection(r.unresolved_content)}
      <div class="card section">
        <div class="row">
          <input id="field-filter" type="search" placeholder="Filter by label or value…" />
          <span class="muted small">Labels and values are shown exactly as detected in this PDF, in reading order.</span>
        </div>
      </div>
      ${pageSections(fields)}
      </div>
      <div id="review-card" class="card section">
        <h3>Review</h3>
        <p class="muted">Only fields you change (in the Fields tab) are saved as corrections; extracted values are never overwritten. Nothing is verified until a reviewer marks it so.</p>
        <div class="row">
          <button class="btn" id="save-corrections">Save changed fields</button>
          <button class="btn secondary" id="reprocess-btn">Reprocess</button>
        </div>
        <div class="row" style="margin-top:0.75rem">
          <input id="verify-notes" type="text" placeholder="Verification notes (required)" />
          <button class="btn secondary" id="verify-btn">Mark verified</button>
        </div>
        <div id="save-msg" class="hidden"></div>
        <details id="history"><summary>Correction history</summary><div id="history-body" class="muted">Loading…</div></details>
      </div>`;

    bindTabs();

    document.getElementById("field-filter").addEventListener("input", (e) => {
      const q = e.target.value.trim().toLowerCase();
      root.querySelectorAll("tr.pdf-field").forEach((tr) => {
        tr.classList.toggle("hidden", Boolean(q) && !tr.dataset.search.includes(q));
      });
    });
    const msg = document.getElementById("save-msg");
    document.getElementById("save-corrections").onclick = async () => {
      const corrections = [];
      root.querySelectorAll(".edit-input").forEach((inp) => {
        if (inp.value !== inp.dataset.initial) {
          corrections.push({ field_id: inp.dataset.fieldId, corrected_value: inp.value, review_status: "corrected" });
        }
      });
      if (!corrections.length) {
        setAlert(msg, "No fields were changed.", "info");
        return;
      }
      try {
        await api(`${docUrl}/fields`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ corrections }),
        });
        setAlert(msg, `${corrections.length} correction(s) saved; originals kept.`, "ok");
        load();
      } catch (err) {
        setAlert(msg, err.message, "error");
      }
    };
    document.getElementById("verify-btn").onclick = async () => {
      const notes = document.getElementById("verify-notes").value.trim();
      if (!notes) {
        setAlert(msg, "Enter verification notes before marking verified.", "error");
        return;
      }
      try {
        await api(`${docUrl}/verify`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ verified: true, notes }),
        });
        load();
      } catch (err) {
        setAlert(msg, err.message, "error");
      }
    };
    document.getElementById("reprocess-btn").onclick = () => reprocess(msg);
    document.getElementById("history").addEventListener("toggle", async (e) => {
      if (!e.target.open) return;
      const body = document.getElementById("history-body");
      try {
        const rows = await api(`${docUrl}/corrections`);
        body.innerHTML = rows.length ? `<table class="field-table"><thead><tr><th>When</th><th>Field</th><th>Extracted</th><th>Corrected / note</th><th>By</th></tr></thead><tbody>${
          rows.map((h) => `<tr><td>${escapeHtml(h.created_at)}</td><td class="mono small">${escapeHtml(h.canonical_name)}</td>
            <td>${escapeHtml(h.original_value ?? "")}</td><td>${escapeHtml(h.corrected_value)}</td><td>${escapeHtml(h.reviewer || "—")}</td></tr>`).join("")
        }</tbody></table>` : "No corrections yet.";
      } catch (err) {
        body.textContent = err.message;
      }
    });
  }

  load();
}

/* ---------- History page ---------- */

function initHistoryPage() {
  bindApiKeyForm();
  const body = document.getElementById("history-body");

  async function load() {
    try {
      const data = await api("/api/v1/documents?limit=100");
      if (!data.items.length) {
        body.innerHTML = `<tr><td colspan="6" class="muted">No documents yet.</td></tr>`;
        return;
      }
      body.innerHTML = data.items.map((d) => `<tr>
          <td><a href="/results/${encodeURIComponent(d.id)}">${escapeHtml(d.original_filename)}</a></td>
          <td>${badge(d.status)}${d.status === "failed" && d.error_message ? `<div class="muted small">${escapeHtml(d.error_message.slice(0, 140))}</div>` : ""}</td>
          <td>${escapeHtml(d.page_count ?? "—")}</td>
          <td>${escapeHtml(d.created_at)}</td>
          <td>${d.verified ? badge("verified", d.verified_by || "") : "—"}</td>
          <td><button class="btn secondary" data-reprocess="${escapeHtml(d.id)}">Reprocess</button></td>
        </tr>`).join("");
      body.querySelectorAll("[data-reprocess]").forEach((btn) => {
        btn.onclick = async () => {
          try {
            await api(`/api/v1/documents/${encodeURIComponent(btn.dataset.reprocess)}/reprocess`, { method: "POST" });
            load();
          } catch (err) {
            alert(err.message);
          }
        };
      });
    } catch (err) {
      body.innerHTML = `<tr><td colspan="6"><div class="alert error">${escapeHtml(err.message)}</div></td></tr>`;
    }
  }
  load();
}

window.ChargeSheetApp = { initUploadPage, initResultsPage, initHistoryPage, bindApiKeyForm };

// Bootstrap from data attributes (no inline scripts, so the CSP can forbid them)
document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  if (page === "upload") initUploadPage();
  else if (page === "results") initResultsPage(document.body.dataset.documentId);
  else if (page === "history") initHistoryPage();
});
