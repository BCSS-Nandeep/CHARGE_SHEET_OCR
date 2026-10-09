/* Charge Sheet UI — talks to local REST API with API key auth */

const API_KEY_STORAGE = "cs_api_key";

function getApiKey() {
  return localStorage.getItem(API_KEY_STORAGE) || "";
}

function setApiKey(value) {
  localStorage.setItem(API_KEY_STORAGE, value || "");
}

function authHeaders(extra = {}) {
  const headers = { ...extra };
  const key = getApiKey();
  if (key) headers["X-API-Key"] = key;
  return headers;
}

async function api(path, options = {}) {
  const opts = { ...options };
  opts.headers = authHeaders(opts.headers || {});
  const res = await fetch(path, opts);
  let body = null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    body = await res.json();
  } else {
    body = await res.text();
  }
  if (!res.ok) {
    const detail =
      (body && body.detail) ||
      (body && body.error) ||
      (typeof body === "string" ? body : res.statusText);
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function badge(status) {
  const s = (status || "unknown").toLowerCase();
  return `<span class="badge ${s}">${s.replace(/_/g, " ")}</span>`;
}

function fieldValue(f) {
  if (!f) return "";
  if (f.corrected_value != null && f.corrected_value !== "") return f.corrected_value;
  if (f.normalized_value != null && f.normalized_value !== "") return f.normalized_value;
  return f.original_value || "";
}

function renderFieldRow(f, editable) {
  if (!f) return "";
  const val = fieldValue(f);
  const orig = f.original_value || "";
  const page = f.source_page != null ? `p.${f.source_page}` : "—";
  const editCell = editable
    ? `<td><input class="edit-input" data-canonical="${escapeAttr(f.canonical_name)}" value="${escapeAttr(String(val))}" /></td>`
    : `<td>${escapeHtml(String(val))}</td>`;
  return `<tr>
    <td class="mono">${escapeHtml(f.canonical_name)}</td>
    <td>${escapeHtml(f.original_label || "—")}</td>
    <td>${escapeHtml(String(orig))}</td>
    ${editCell}
    <td>${page}</td>
    <td>${badge(f.validation_status)}</td>
    <td>${badge(f.review_status)}</td>
  </tr>`;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/'/g, "&#39;");
}

function collectFields(obj, out = []) {
  if (!obj || typeof obj !== "object") return out;
  if (obj.canonical_name && ("original_value" in obj || "normalized_value" in obj)) {
    out.push(obj);
    return out;
  }
  if (Array.isArray(obj)) {
    obj.forEach((x) => collectFields(x, out));
    return out;
  }
  Object.values(obj).forEach((v) => collectFields(v, out));
  return out;
}

function bindApiKeyForm() {
  const input = document.getElementById("api-key-input");
  const saveBtn = document.getElementById("api-key-save");
  if (!input) return;
  input.value = getApiKey();
  if (saveBtn) {
    saveBtn.addEventListener("click", () => {
      setApiKey(input.value.trim());
      const msg = document.getElementById("api-key-msg");
      if (msg) {
        msg.textContent = "API key saved locally in this browser.";
        msg.className = "alert ok";
      }
    });
  }
}

/* ---------- Upload page ---------- */
function initUploadPage() {
  bindApiKeyForm();
  const zone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const uploadBtn = document.getElementById("upload-btn");
  const statusEl = document.getElementById("upload-status");
  const progress = document.getElementById("progress");
  const bar = progress ? progress.querySelector("span") : null;
  let selected = null;

  function setStatus(html, cls) {
    if (!statusEl) return;
    statusEl.innerHTML = html;
    statusEl.className = `alert ${cls || "info"}`;
    statusEl.classList.remove("hidden");
  }

  function showFile(file) {
    selected = file;
    document.getElementById("file-meta").innerHTML = `
      <div class="meta-grid">
        <div class="meta-item"><div class="label">Name</div><div class="value">${escapeHtml(file.name)}</div></div>
        <div class="meta-item"><div class="label">Size</div><div class="value">${formatBytes(file.size)}</div></div>
        <div class="meta-item"><div class="label">Type</div><div class="value">${escapeHtml(file.type || "application/pdf")}</div></div>
      </div>`;
    uploadBtn.disabled = false;
  }

  zone.addEventListener("click", () => fileInput.click());
  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("dragover");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("dragover");
    const f = e.dataTransfer.files[0];
    if (f) showFile(f);
  });
  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) showFile(fileInput.files[0]);
  });

  uploadBtn.addEventListener("click", async () => {
    if (!selected) return;
    if (!selected.name.toLowerCase().endsWith(".pdf")) {
      setStatus("Only PDF files are accepted.", "error");
      return;
    }
    uploadBtn.disabled = true;
    progress.style.display = "block";
    bar.style.width = "15%";
    setStatus("Uploading…", "info");
    try {
      const fd = new FormData();
      fd.append("file", selected);
      bar.style.width = "45%";
      const res = await api("/api/v1/documents/upload", { method: "POST", body: fd });
      bar.style.width = "100%";
      setStatus(
        `Accepted. Document ID: <span class="mono">${escapeHtml(res.document_id)}</span> — status: ${badge(res.status)}. <a href="/results/${encodeURIComponent(res.document_id)}">Open results</a>`,
        "ok"
      );
      setTimeout(() => {
        window.location.href = `/results/${encodeURIComponent(res.document_id)}`;
      }, 800);
    } catch (err) {
      setStatus(escapeHtml(err.message || "Upload failed"), "error");
      uploadBtn.disabled = false;
      bar.style.width = "0%";
    }
  });
}

/* ---------- Results page ---------- */
function initResultsPage(documentId) {
  bindApiKeyForm();
  const root = document.getElementById("results-root");
  const pollNote = document.getElementById("poll-note");

  async function load() {
    try {
      const meta = await api(`/api/v1/documents/${documentId}`);
      const doc = meta.document;
      renderMeta(doc);

      if (["queued", "processing", "uploaded"].includes(doc.status)) {
        pollNote.classList.remove("hidden");
        pollNote.textContent = `Processing… status=${doc.status}. Refreshing automatically.`;
        setTimeout(load, 2000);
        return;
      }
      pollNote.classList.add("hidden");

      if (doc.status === "failed") {
        root.innerHTML = `<div class="alert error">Processing failed: ${escapeHtml(doc.error_message || "unknown error")}.
          <button class="btn secondary" id="retry-btn">Retry</button></div>`;
        document.getElementById("retry-btn").onclick = async () => {
          await api(`/api/v1/documents/${documentId}/reprocess`, { method: "POST" });
          load();
        };
        return;
      }

      const payload = await api(`/api/v1/documents/${documentId}/results`);
      renderResults(payload);
    } catch (err) {
      root.innerHTML = `<div class="alert error">${escapeHtml(err.message)}</div>`;
    }
  }

  function renderMeta(doc) {
    document.getElementById("doc-meta").innerHTML = `
      <div class="meta-grid">
        <div class="meta-item"><div class="label">Document ID</div><div class="value mono">${escapeHtml(doc.id)}</div></div>
        <div class="meta-item"><div class="label">Filename</div><div class="value">${escapeHtml(doc.original_filename)}</div></div>
        <div class="meta-item"><div class="label">Status</div><div class="value">${badge(doc.status)}</div></div>
        <div class="meta-item"><div class="label">Pages</div><div class="value">${doc.page_count ?? "—"}</div></div>
        <div class="meta-item"><div class="label">Scanned</div><div class="value">${doc.is_scanned == null ? "—" : doc.is_scanned}</div></div>
        <div class="meta-item"><div class="label">Confidence</div><div class="value">${doc.classification_confidence ?? "—"}</div></div>
        <div class="meta-item"><div class="label">Uploaded</div><div class="value">${escapeHtml(doc.created_at)}</div></div>
        <div class="meta-item"><div class="label">Source PDF</div><div class="value"><a href="/api/v1/documents/${encodeURIComponent(doc.id)}/source" target="_blank" id="source-link">Open PDF</a></div></div>
      </div>`;
    const link = document.getElementById("source-link");
    if (link) {
      link.addEventListener("click", async (e) => {
        e.preventDefault();
        try {
          const res = await fetch(link.href, { headers: authHeaders() });
          if (!res.ok) throw new Error("Unable to open source PDF");
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          window.open(url, "_blank");
        } catch (err) {
          alert(err.message);
        }
      });
    }
  }

  function personDetail(label, f) {
    if (!f || (f.original_value == null && f.normalized_value == null && f.corrected_value == null)) return "";
    const alts = (f.alternate_values || []).length
      ? ` <span class="muted">(alts: ${escapeHtml((f.alternate_values || []).join(" | "))})</span>`
      : "";
    const page = f.source_page != null ? ` · p.${f.source_page}` : "";
    return `<div class="person-line"><span class="muted">${escapeHtml(label)}:</span> ${escapeHtml(String(fieldValue(f)))}${alts} ${badge(f.validation_status)} ${badge(f.review_status)}<span class="muted">${page}</span></div>
      ${f.source_text ? `<div class="source-snippet muted">src: ${escapeHtml(String(f.source_text).slice(0, 160))}</div>` : ""}`;
  }

  function peopleSection(title, people, kind) {
    if (!people || !people.length) {
      return `<div class="card section"><h3>${title}</h3><p class="muted">None extracted (not fabricated).</p></div>`;
    }
    const cards = people
      .map((p, i) => {
        const name = fieldValue(p.name) || "(no name)";
        const sno = fieldValue(p.serial_no);
        return `<div class="person-card">
          <strong>${sno ? escapeHtml(String(sno)) + ". " : ""}${escapeHtml(String(name))}</strong>
          <div class="muted mono">${kind}[${i}]</div>
          ${personDetail("Age", p.age)}
          ${personDetail("Parent/spouse", p.parent_or_spouse)}
          ${personDetail("Occupation", p.occupation)}
          ${personDetail("Address", p.address)}
          ${personDetail("ID", p.identification)}
          ${personDetail("Role/type", p.witness_type || p.role)}
        </div>`;
      })
      .join("");
    return `<div class="card section"><h3>${title} (${people.length})</h3>${cards}</div>`;
  }

  function conflictsSection(conflicts) {
    if (!conflicts || !conflicts.length) return "";
    const items = conflicts
      .map(
        (c) => `<li><strong>${escapeHtml(c.field_name)}</strong>: ${escapeHtml((c.values || []).join(" vs "))}
        <div class="muted">${escapeHtml(c.evidence || "")} · resolution=${escapeHtml(c.resolution || "unresolved")}</div></li>`
      )
      .join("");
    return `<div class="card section"><h3>Source conflicts (unresolved)</h3>
      <p class="muted">These disagreements exist in the PDF text and are not auto-resolved.</p>
      <ul class="warnings">${items}</ul></div>`;
  }

  function sectionsBlock(sections) {
    if (!sections || !sections.length) {
      return `<div class="card section"><h3>Acts & sections</h3><p class="muted">None extracted.</p></div>`;
    }
    const rows = sections
      .map((s) => {
        const act = fieldValue(s.act) || "—";
        const sec = fieldValue(s.section) || "—";
        const page = (s.section && s.section.source_page) || (s.act && s.act.source_page) || "—";
        return `<tr><td>${escapeHtml(String(act))}</td><td>${escapeHtml(String(sec))}</td><td>p.${page}</td>
          <td>${s.section ? badge(s.section.validation_status) : ""}</td></tr>`;
      })
      .join("");
    return `<div class="card section"><h3>Acts & sections</h3>
      <table class="field-table"><thead><tr><th>Act</th><th>Section</th><th>Page</th><th>Status</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  function renderResults(payload) {
    const r = payload.results;
    if (!r) {
      root.innerHTML = `<div class="alert info">No results payload yet.</div>`;
      return;
    }
    // Prefer case + people fields in table; keep unmapped separate
    const caseFields = collectFields(r.case || {});
    const other = collectFields({
      complainant: r.complainant,
      informant: r.informant,
      investigating_officer: r.investigating_officer,
      brief_facts: r.brief_facts,
      investigation_narrative: r.investigation_narrative,
      final_conclusions: r.final_conclusions,
      seized_property: r.seized_property,
      lab_results: r.lab_results,
    });
    const unmapped = r.unmapped_fields || [];
    const rows = [...caseFields, ...other].map((f) => renderFieldRow(f, true)).join("");
    const unmappedRows = unmapped.map((f) => renderFieldRow(f, true)).join("");
    const warnings = (r.extraction_warnings || [])
      .map((w) => `<li>${escapeHtml(w)}</li>`)
      .join("");
    const stages = (r.pipeline_stages || []).join(" → ");

    root.innerHTML = `
      <div class="card">
        <h2>Extraction summary</h2>
        <div class="meta-grid">
          <div class="meta-item"><div class="label">Schema</div><div class="value">${escapeHtml(r.schema_version)}</div></div>
          <div class="meta-item"><div class="label">Confidence (not accuracy)</div><div class="value">${r.overall_confidence ?? "—"}</div></div>
          <div class="meta-item"><div class="label">Doc type</div><div class="value">${escapeHtml(fieldValue(r.document_info?.document_type) || "—")}</div></div>
          <div class="meta-item"><div class="label">Needs review</div><div class="value">${r.requires_human_review}</div></div>
          <div class="meta-item"><div class="label">Conflicts</div><div class="value">${(r.source_conflicts || []).length}</div></div>
          <div class="meta-item"><div class="label">Pipeline</div><div class="value" style="font-size:0.8rem">${escapeHtml(stages || "—")}</div></div>
        </div>
        ${warnings ? `<ul class="warnings">${warnings}</ul>` : ""}
      </div>
      ${conflictsSection(r.source_conflicts)}
      ${peopleSection("Accused persons", r.accused_persons, "accused")}
      ${peopleSection("Witnesses", r.witnesses, "witness")}
      ${sectionsBlock(r.acts_and_sections)}
      <div class="card section">
        <h3>Case & other mapped fields</h3>
        <p class="muted">Original values preserved. Corrections stored separately. Confidence ≠ measured accuracy.</p>
        <table class="field-table">
          <thead><tr>
            <th>Canonical</th><th>Label</th><th>Original</th><th>Value / correction</th><th>Page</th><th>Validation</th><th>Review</th>
          </tr></thead>
          <tbody>${rows || `<tr><td colspan="7" class="muted">No fields</td></tr>`}</tbody>
        </table>
      </div>
      <div class="card section">
        <h3>Unmapped / unrecognized</h3>
        <p class="muted">Preserved without forcing into schema fields.</p>
        <table class="field-table">
          <thead><tr>
            <th>Canonical</th><th>Label</th><th>Original</th><th>Value / correction</th><th>Page</th><th>Validation</th><th>Review</th>
          </tr></thead>
          <tbody>${unmappedRows || `<tr><td colspan="7" class="muted">None</td></tr>`}</tbody>
        </table>
        <div class="row" style="margin-top:1rem; gap:0.5rem; flex-wrap:wrap; align-items:center;">
          <input id="verify-notes" type="text" placeholder="Verification notes (required to mark verified)" style="max-width:420px" />
          <button class="btn" id="save-corrections">Save corrections</button>
          <button class="btn secondary" id="verify-btn">Mark verified</button>
          <button class="btn secondary" id="reprocess-btn">Reprocess</button>
        </div>
        <div id="save-msg" class="hidden"></div>
      </div>`;

    document.getElementById("save-corrections").onclick = async () => {
      const inputs = root.querySelectorAll("input.edit-input");
      const corrections = [];
      inputs.forEach((inp) => {
        corrections.push({
          canonical_name: inp.dataset.canonical,
          corrected_value: inp.value,
          review_status: "corrected",
        });
      });
      const msg = document.getElementById("save-msg");
      try {
        await api(`/api/v1/documents/${documentId}/fields`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ corrections }),
        });
        msg.className = "alert ok";
        msg.textContent = "Corrections saved (originals retained; audit trail kept).";
        msg.classList.remove("hidden");
        load();
      } catch (err) {
        msg.className = "alert error";
        msg.textContent = err.message;
        msg.classList.remove("hidden");
      }
    };

    document.getElementById("verify-btn").onclick = async () => {
      const notes = (document.getElementById("verify-notes").value || "").trim();
      const msg = document.getElementById("save-msg");
      if (!notes) {
        msg.className = "alert error";
        msg.textContent = "Enter verification notes before marking verified.";
        msg.classList.remove("hidden");
        return;
      }
      await api(`/api/v1/documents/${documentId}/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ verified: true, notes }),
      });
      load();
    };

    document.getElementById("reprocess-btn").onclick = async () => {
      await api(`/api/v1/documents/${documentId}/reprocess`, { method: "POST" });
      load();
    };
  }

  load();
}

/* ---------- History page ---------- */
function initHistoryPage() {
  bindApiKeyForm();
  const body = document.getElementById("history-body");

  async function load() {
    try {
      const data = await api("/api/v1/documents?limit=50");
      if (!data.items.length) {
        body.innerHTML = `<tr><td colspan="6" class="muted">No documents yet.</td></tr>`;
        return;
      }
      body.innerHTML = data.items
        .map(
          (d) => `<tr>
          <td class="mono"><a href="/results/${encodeURIComponent(d.id)}">${escapeHtml(d.id.slice(0, 8))}…</a></td>
          <td>${escapeHtml(d.original_filename)}</td>
          <td>${badge(d.status)}</td>
          <td>${escapeHtml(d.created_at)}</td>
          <td>${d.verified ? badge("verified") : "—"}</td>
          <td>
            <a href="/results/${encodeURIComponent(d.id)}">Open</a>
            ${d.status === "failed" ? ` · <button class="btn secondary" data-retry="${d.id}">Retry</button>` : ""}
          </td>
        </tr>`
        )
        .join("");
      body.querySelectorAll("[data-retry]").forEach((btn) => {
        btn.onclick = async () => {
          await api(`/api/v1/documents/${btn.dataset.retry}/reprocess`, { method: "POST" });
          load();
        };
      });
    } catch (err) {
      body.innerHTML = `<tr><td colspan="6" class="alert error">${escapeHtml(err.message)}</td></tr>`;
    }
  }
  load();
}

window.ChargeSheetApp = {
  initUploadPage,
  initResultsPage,
  initHistoryPage,
  bindApiKeyForm,
};
