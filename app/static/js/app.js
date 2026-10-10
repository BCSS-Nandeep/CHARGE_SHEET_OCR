/* Charge Sheet UI — talks to the local REST API. */

const API_KEY_STORAGE = "cs_api_key";
const API_KEY_HEADER = (document.body && document.body.dataset.apiKeyHeader) || "X-API-Key";
const CORE_CASE_FIELDS = [
  "district", "police_station", "year", "fir_number", "fir_date",
  "charge_sheet_number", "submission_date", "court_name", "final_report_type", "supplementary_or_original",
];

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

function badge(status, title) {
  const s = String(status || "unknown").toLowerCase().replace(/[^a-z_]/g, "");
  return `<span class="badge ${s}"${title ? ` title="${escapeHtml(title)}"` : ""}>${escapeHtml(s.replace(/_/g, " "))}</span>`;
}

function shownValue(f) {
  if (!f) return "";
  if (f.corrected_value != null && f.corrected_value !== "") return String(f.corrected_value);
  return f.original_value == null ? "" : String(f.original_value);
}

function humanize(name) {
  // "accused[0].parent_or_spouse" → "accused 1 parent or spouse"
  return String(name)
    .replace(/^unmapped\[\d+\]\./, "")
    .replace(/\[(\d+)\]\.?/g, (m, i) => ` ${Number(i) + 1} `)
    .replace(/[._]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
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
        h.offline_mode ? "offline mode" : "network allowed",
      ];
      status.textContent = parts.join(" · ");
      if (!h.ocr_available || h.auth_misconfigured) status.classList.add("warn-text");
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
    if (!selected.name.toLowerCase().endsWith(".pdf")) {
      setAlert(statusEl, "Only PDF files are accepted.", "error");
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
      pollNote.textContent = `Processing… (status: ${doc.status}). Scanned pages are OCR'd locally and can take about a minute per page. This page refreshes automatically.`;
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
        <div class="meta-item"><div class="label">Source PDF</div><div class="value"><a href="#" id="source-link">Open PDF</a></div></div>
      </div>`;
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
    const rows = pages.map((p) => `<tr><td>${p.page_number}</td><td>${badge(p.text_source === "none" ? "missing" : p.text_source)}</td>
      <td>${p.char_count}</td><td>${p.ocr_confidence == null ? "—" : escapeHtml(p.ocr_confidence)}</td><td>${escapeHtml(p.note || "")}</td></tr>`).join("");
    return `<h3>Pages</h3><table class="field-table"><thead><tr><th>Page</th><th>Text from</th><th>Characters</th><th>OCR confidence</th><th>Note</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function warningsList(warnings) {
    if (!warnings || !warnings.length) return "";
    return `<ul class="warnings">${warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul>`;
  }

  /* A field row: label, value (editable), statuses, page, source text */
  function fieldRow(label, f, canonical) {
    if (!f) {
      return `<tr class="missing-row"><td>${escapeHtml(label)}</td><td class="muted">not found in the source</td>
        <td>${badge("missing")}</td><td>—</td><td></td></tr>`;
    }
    const value = shownValue(f);
    const corrected = f.corrected_value != null && f.corrected_value !== "";
    const alts = (f.alternate_values || []).length
      ? `<div class="muted small">other values in source: ${escapeHtml(f.alternate_values.join(" | "))}</div>` : "";
    const notes = f.notes ? `<div class="muted small">${escapeHtml(f.notes)}</div>` : "";
    const original = corrected ? `<div class="muted small">extracted: ${escapeHtml(f.original_value ?? "")}</div>` : "";
    const src = f.source_text
      ? `<details><summary>source</summary><div class="source-snippet">${escapeHtml(f.source_text)}</div></details>` : "";
    const input = value.length > 120
      ? `<textarea class="edit-input" data-canonical="${escapeHtml(canonical || f.canonical_name)}" data-initial="${escapeHtml(value)}">${escapeHtml(value)}</textarea>`
      : `<input type="text" class="edit-input" data-canonical="${escapeHtml(canonical || f.canonical_name)}" data-initial="${escapeHtml(value)}" value="${escapeHtml(value)}" />`;
    return `<tr>
      <td>${escapeHtml(label)}${f.original_label ? `<div class="muted small">label: ${escapeHtml(f.original_label)}</div>` : ""}</td>
      <td>${input}${original}${alts}${notes}</td>
      <td>${badge(f.validation_status)} ${badge(corrected ? "corrected" : f.review_status)}</td>
      <td>${f.source_page != null ? `p.${escapeHtml(f.source_page)}` : "—"}</td>
      <td>${src}</td>
    </tr>`;
  }

  function fieldTable(rows) {
    return `<table class="field-table"><thead><tr><th>Field</th><th>Value</th><th>Status</th><th>Page</th><th>Source</th></tr></thead><tbody>${rows || `<tr><td colspan="5" class="muted">None</td></tr>`}</tbody></table>`;
  }

  const PERSON_ATTRS = [
    ["serial_no", "Serial"], ["name", "Name"], ["parent_or_spouse", "Parent / spouse"], ["age", "Age"],
    ["occupation", "Occupation / rank"], ["address", "Address"], ["contact", "Contact"],
    ["identification", "ID number"], ["witness_type", "Role"], ["role", "Role"], ["arrest_status", "Arrest / custody"],
  ];

  function personCards(title, people) {
    if (!people || !people.length) {
      return `<div class="card section"><h3>${escapeHtml(title)}</h3><p class="muted">None found in the source (nothing was invented).</p></div>`;
    }
    const cards = people.map((p, i) => {
      const rows = PERSON_ATTRS.filter(([k]) => p[k]).map(([k, label]) => fieldRow(label, p[k])).join("")
        + (p.additional_fields || []).map((f) => fieldRow(humanize(f.canonical_name.split(".").pop()), f)).join("");
      const heading = `${shownValue(p.serial_no) ? escapeHtml(shownValue(p.serial_no)) + ". " : ""}${escapeHtml(shownValue(p.name) || "(no name)")}`;
      const conflict = Object.values(p).some((f) => f && f.validation_status === "conflicting") ? ` ${badge("conflicting")}` : "";
      return `<details class="person-card" ${i < 3 ? "open" : ""}><summary><strong>${heading}</strong>${conflict}</summary>${fieldTable(rows)}</details>`;
    }).join("");
    return `<div class="card section"><h3>${escapeHtml(title)} (${people.length})</h3>${cards}</div>`;
  }

  function legalTable(title, sections, showRole) {
    if (!sections || !sections.length) {
      return `<div class="card section"><h3>${escapeHtml(title)}</h3><p class="muted">None found.</p></div>`;
    }
    const rows = sections.map((s) => {
      const pages = (s.source_pages || []).map((p) => `p.${p}`).join(", ") || "—";
      return `<tr><td>${escapeHtml(shownValue(s.act) || "—")}</td><td>${escapeHtml(shownValue(s.section) || "—")}</td>
        ${showRole ? `<td>${badge(s.provision_role || "reference")}</td>` : ""}
        <td>${s.act ? badge(s.act.validation_status) : ""}</td><td>${escapeHtml(pages)}</td>
        <td>${s.section && s.section.source_text ? `<details><summary>source</summary><div class="source-snippet">${escapeHtml(s.section.source_text)}</div></details>` : ""}</td></tr>`;
    }).join("");
    return `<div class="card section"><h3>${escapeHtml(title)}</h3>
      <table class="field-table"><thead><tr><th>Act</th><th>Section</th>${showRole ? "<th>Role</th>" : ""}<th>Status</th><th>Pages</th><th>Source</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  function conflictsSection(conflicts) {
    if (!conflicts || !conflicts.length) return "";
    const items = conflicts.map((c) => `<li><strong>${escapeHtml(humanize(c.field_name))}</strong> <span class="mono small">${escapeHtml(c.field_name)}</span>:
      ${(c.values || []).map((v) => `<code>${escapeHtml(v)}</code>`).join(" vs ")}
      <div class="muted small">${escapeHtml(c.evidence || "")} · pages ${escapeHtml((c.source_pages || []).join(", ") || "—")} · ${escapeHtml(c.resolution || "unresolved")}</div></li>`).join("");
    return `<div class="card section"><h3>Source conflicts (${conflicts.length}, unresolved)</h3>
      <p class="muted">The source document itself disagrees on these values. Nothing was chosen automatically — confirm the right value below.</p>
      <ul class="conflicts">${items}</ul></div>`;
  }

  function renderResults(doc, payload) {
    const r = payload.results;
    if (!r) {
      root.innerHTML = `<div class="alert info">No results yet.</div>`;
      return;
    }
    const banner = r.extraction_status === "partial"
      ? `<div class="alert warn"><strong>Partial extraction.</strong> Some pages or stages failed; fields from those pages are missing. See pipeline stages below.</div>`
      : "";
    const caseRows = CORE_CASE_FIELDS.concat(Object.keys(r.case || {}).filter((k) => !CORE_CASE_FIELDS.includes(k)))
      .filter((k, i, a) => a.indexOf(k) === i)
      .map((k) => fieldRow(humanize(k), r.case ? r.case[k] : null, k)).join("");
    const people = [
      ["Complainant", r.complainant ? [r.complainant] : []],
      ["Investigating officers", r.investigating_officers || []],
    ];
    const narrative = ["brief_facts", "investigation_narrative", "final_conclusions"]
      .filter((k) => r[k]).map((k) => fieldRow(humanize(k), r[k])).join("");
    const evidence = (r.seized_property || []).map((s) => fieldRow("seized property", s.description)).join("")
      + (r.lab_results || []).map((l) => fieldRow("lab finding", l.findings)).join("");
    const related = (r.related_case_references || []).map((f) => fieldRow("other case (narrative)", f)).join("");
    const unmapped = (r.unmapped_fields || []).map((f) => fieldRow(f.original_label || humanize(f.canonical_name), f)).join("");

    root.innerHTML = `
      <div class="card">
        <h2>Extraction summary</h2>
        ${banner}
        <div class="meta-grid">
          <div class="meta-item"><div class="label">Extraction</div><div class="value">${badge(r.extraction_status)}</div></div>
          <div class="meta-item"><div class="label">Document type</div><div class="value">${escapeHtml(shownValue(r.document_info?.document_type) || "—")}</div></div>
          <div class="meta-item"><div class="label">Model confidence (not accuracy)</div><div class="value">${escapeHtml(r.overall_confidence ?? "—")}</div></div>
          <div class="meta-item"><div class="label">Conflicts</div><div class="value">${(r.source_conflicts || []).length}</div></div>
          <div class="meta-item"><div class="label">Schema</div><div class="value">${escapeHtml(r.schema_version)}</div></div>
        </div>
        ${warningsList(r.extraction_warnings)}
        <details><summary>Pipeline stages and pages</summary>${stagesTable(r.stage_results)}${pagesTable(r.page_summaries)}</details>
      </div>
      ${conflictsSection(r.source_conflicts)}
      <div class="card section"><h3>Case details</h3>${fieldTable(caseRows)}</div>
      ${legalTable("Acts & sections (applicable)", r.acts_and_sections, true)}
      ${personCards("Accused persons", r.accused_persons)}
      ${personCards("Witnesses", r.witnesses)}
      ${people.map(([t, list]) => personCards(t, list)).join("")}
      ${legalTable("Other legal references (procedural / superseded / cited)", r.other_legal_references, true)}
      <div class="card section"><h3>Other cases mentioned in the narrative</h3>
        <p class="muted">Not the primary FIR — kept separate so they never overwrite case details.</p>${fieldTable(related)}</div>
      <div class="card section"><h3>Narrative & evidence</h3>${fieldTable(narrative + evidence)}</div>
      <div class="card section"><h3>Unmapped form fields</h3><p class="muted">Recognised in the form but not part of the schema — preserved as found.</p>${fieldTable(unmapped)}</div>
      <div class="card section">
        <h3>Review</h3>
        <p class="muted">Only fields you change are saved as corrections; extracted values are never overwritten.</p>
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

    const msg = document.getElementById("save-msg");
    document.getElementById("save-corrections").onclick = async () => {
      const corrections = [];
      root.querySelectorAll(".edit-input").forEach((inp) => {
        if (inp.value !== inp.dataset.initial) {
          corrections.push({ canonical_name: inp.dataset.canonical, corrected_value: inp.value, review_status: "corrected" });
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
