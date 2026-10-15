/* Results-page additions of the unified application (added to the host's and the charge-sheet service's own pages
 * by the gateway; neither page's own files are edited):
 *   1. a banner with the document type, how it was decided, both pipelines' statuses, warnings and the combined JSON;
 *   2. the Person Images tab (same API and behaviour as the IR_OCR person-image integration), working with the tab
 *      bars of both pages.
 * It only calls the unified / person-image API and never touches the OCR review controls.
 */
(function () {
  "use strict";
  const body = document.body;
  const docId = body && body.dataset.documentId;
  if (!docId || (body.dataset.page || "") !== "results") return;

  const API = `/api/v1/documents/${encodeURIComponent(docId)}`;
  const HEADER = body.dataset.apiKeyHeader || "X-API-Key";
  const root = document.getElementById("results-root");
  if (!root) return;
  const TYPE = { charge_sheet: "Charge sheet", interrogation_report: "Interrogation report" };
  const SOURCE = { auto: "detected automatically", manual: "selected by the user", legacy: "uploaded before the merge (IR_OCR)" };

  let view = null, coord = null, timer = null, bannerTimer = null, blobUrls = [], active = false;

  /* the two pages keep the API key under different names; use whichever has it and keep both in step */
  function key() {
    try {
      const k = sessionStorage.getItem("ir_api_key") || sessionStorage.getItem("cs_api_key") || "";
      if (k) { sessionStorage.setItem("ir_api_key", k); sessionStorage.setItem("cs_api_key", k); }
      return k;
    } catch (e) { return ""; }
  }
  function headers() { const h = {}; const k = key(); if (k) h[HEADER] = k; return h; }
  function esc(s) {
    return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  async function getJson(url, opts) {
    const res = await fetch(url, { ...(opts || {}), headers: { ...headers(), ...((opts && opts.headers) || {}) } });
    const ct = res.headers.get("content-type") || "";
    const data = ct.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
      const d = data && data.detail !== undefined ? data.detail : data;
      throw new Error(typeof d === "string" ? d : (d && d.message) || res.statusText);
    }
    return data;
  }
  function badge(status) {
    const s = String(status || "unknown").toLowerCase().replace(/[^a-z_]/g, "");
    const cls = s === "succeeded" ? "completed" : s === "no_person_images" ? "skipped" : s;
    return `<span class="badge ${cls}">${esc(s.replace(/_/g, " "))}</span>`;
  }
  function download(name, data) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: name });
    document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  }

  /* ------------------------------------------------------------ banner */
  function renderBanner() {
    let el = document.getElementById("u-banner");
    if (!el) {
      el = document.createElement("div");
      el.id = "u-banner";
      el.className = "card u-banner";
      root.parentElement.insertBefore(el, root);
    }
    const c = coord;
    if (!c) { el.innerHTML = `<span class="muted small">Loading document routing…</span>`; return; }
    const ocr = c.pipelines.ocr, img = c.pipelines.person_images;
    const prog = ocr.progress ? ` · ${esc(ocr.progress.step)} (${Math.round(ocr.progress.elapsed_seconds)} s)` : "";
    const notes = [...(c.warnings || []).map((w) => ["warn", w]), ...(c.errors || []).map((w) => ["error", w])]
      .map(([k, w]) => `<div class="alert ${k}">${esc(w.stage)}: ${esc(w.message)}</div>`).join("");
    el.innerHTML = `
      <div class="row u-row">
        <strong>${esc(TYPE[c.document_type] || c.document_type)}</strong>
        <span class="muted small">${esc(SOURCE[c.document_type_source] || c.document_type_source)} · extractor ${esc(c.provenance && c.provenance.implementation)}</span>
        <span class="u-spacer"></span>
        <button class="btn secondary" id="u-report" type="button">Download combined JSON</button>
      </div>
      <div class="row u-row small">
        <span>OCR ${badge(ocr.status)}${prog}${ocr.error ? ` <span class="muted">${esc(ocr.error)}</span>` : ""}</span>
        ${img.status === "not_applicable" ? "" : `<span>Person images ${badge(img.status)}</span>`}
        <span>Review ${badge(c.review.verified ? "verified" : (ocr.status === "processing" ? "pending" : "needs_review"))}${c.review.corrected_fields ? ` <span class="muted">${c.review.corrected_fields} corrected field(s)</span>` : ""}</span>
      </div>${notes}`;
    el.querySelector("#u-report").onclick = async () => {
      try { download("report_result.json", await getJson(`${API}/report_result`)); }
      catch (e) { alert(e.message); }
    };
  }
  async function refreshBanner() {
    try { coord = await getJson(`/api/v1/unified/documents/${encodeURIComponent(docId)}`); } catch (e) { coord = null; }
    renderBanner();
    mount();
    clearTimeout(bannerTimer);
    if (coord && (coord.pipelines.ocr.status === "processing" || ["queued", "processing"].includes(coord.pipelines.person_images.status))) {
      bannerTimer = setTimeout(refreshBanner, 4000);
    }
  }

  /* ----------------------------------------------------- person images */
  const STATUS_TEXT = {
    not_started: "Image extraction has not started for this document.",
    queued: "Image extraction is queued…",
    processing: "Extracting person images…",
    succeeded: "",
    partial: "Some images in this document could not be read; the person images found are shown.",
    no_person_images: "No person images were found in this document.",
    failed: "Image extraction failed. OCR results are not affected.",
    unsupported: "This file type is not supported by the image extraction (PDF and DOCX only).",
  };
  function releaseBlobs() { blobUrls.forEach((u) => URL.revokeObjectURL(u)); blobUrls = []; }
  async function loadImage(img, url) {
    try {
      const res = await fetch(url, { headers: headers() });
      if (!res.ok) throw new Error(String(res.status));
      const u = URL.createObjectURL(await res.blob());
      blobUrls.push(u); img.src = u;
    } catch (e) {
      img.replaceWith(Object.assign(document.createElement("div"), { className: "pi-missing", textContent: "Image file not available" }));
    }
  }
  function render(host) {
    releaseBlobs();
    const v = view || { status: "not_started", images: [] };
    const images = v.images || [];
    const note = v.status === "failed" && v.error ? `${STATUS_TEXT.failed} (${esc(v.error)})` : (STATUS_TEXT[v.status] || "");
    const tone = v.status === "failed" ? "error" : (v.status === "partial" ? "warn" : "info");
    const stats = images.length || v.total_images_found
      ? `<span class="muted small">${images.length} person image${images.length === 1 ? "" : "s"} · ${v.total_images_found || 0} image(s) found in the document · ${v.skipped_images || 0} skipped as not showing a person${v.unreadable_images ? ` · ${v.unreadable_images} unreadable` : ""}</span>` : "";
    host.innerHTML = `
      <div class="card section pi-card">
        <div class="row pi-head">
          <h3>Person images</h3> ${badge(v.status)}
          <button class="btn secondary" id="pi-rerun" type="button">Run image extraction again</button>
          <button class="btn secondary" id="pi-report" type="button">Download report_result.json</button>
        </div>
        ${stats}
        ${note ? `<div class="alert ${tone}">${note}</div>` : ""}
        <div id="pi-msg" class="hidden"></div>
        <div class="pi-grid">
          ${images.map((im, i) => `
            <figure class="pi-fig">
              ${im.available && im.url ? `<img alt="Person image ${i + 1}" data-url="${esc(im.url)}" />` : `<div class="pi-missing">Image file not available</div>`}
              <figcaption><strong>${esc(im.name)}</strong>
                <span class="muted small">${im.source_page ? `page ${esc(im.source_page)}` : esc(im.source_member || "embedded image")} · ${esc(im.width)}×${esc(im.height)} px · ${esc(im.faces)} face${im.faces === 1 ? "" : "s"}</span>
              </figcaption>
            </figure>`).join("")}
        </div>
      </div>`;
    host.querySelectorAll("img[data-url]").forEach((img) => loadImage(img, img.dataset.url));
    const msg = host.querySelector("#pi-msg");
    const flash = (text, kind) => { msg.className = `alert ${kind}`; msg.textContent = text; };
    host.querySelector("#pi-rerun").onclick = async () => {
      try { await getJson(`${API}/person-images/run`, { method: "POST" }); flash("Image extraction started.", "info"); await refresh(); }
      catch (e) { flash(e.message, "error"); }
    };
    host.querySelector("#pi-report").onclick = async () => {
      try { download("report_result.json", await getJson(`${API}/report_result`)); } catch (e) { flash(e.message, "error"); }
    };
  }
  function panelHost() { return document.getElementById("pi-panel") || document.getElementById("pi-standalone"); }
  async function refresh() {
    try { view = await getJson(`${API}/person-images`); } catch (e) { view = { status: "failed", error: e.message, images: [] }; }
    const h = panelHost(); if (h) render(h);
    clearTimeout(timer);
    if (view && (view.status === "queued" || view.status === "processing")) { timer = setTimeout(refresh, 3000); refreshBanner(); }
  }
  function select(on) {
    active = on;
    const panel = document.getElementById("pi-panel"), tab = document.getElementById("pi-tab");
    if (!panel || !tab) return;
    panel.classList.toggle("hidden", !on);
    if (on) {
      // the IR page has tab-fields / tab-raw, the charge-sheet page tab-fields / tab-json: hide whichever exist
      root.querySelectorAll('[id^="tab-"]').forEach((el) => el.classList.add("hidden"));
      root.querySelectorAll(".tab").forEach((b) => { b.classList.toggle("active", b === tab); b.setAttribute("aria-selected", String(b === tab)); });
      refresh();
    }
  }
  function mount() {
    // Charge sheets get no person-image extraction: no tab there. Until the document type is known, add nothing.
    if (!coord || coord.document_type === "charge_sheet") return;
    const tabs = root.querySelector(".tabs");
    const standalone = document.getElementById("pi-standalone");
    if (tabs) {
      if (standalone) standalone.remove();
      if (!document.getElementById("pi-tab")) {
        const btn = document.createElement("button");
        btn.className = "tab"; btn.id = "pi-tab"; btn.type = "button";
        btn.setAttribute("role", "tab"); btn.setAttribute("aria-selected", "false"); btn.textContent = "Person Images";
        tabs.appendChild(btn);
        const panel = document.createElement("div");
        panel.id = "pi-panel"; panel.className = "hidden";
        tabs.insertAdjacentElement("afterend", panel);
        tabs.addEventListener("click", (e) => {
          const target = e.target.closest(".tab");
          if (!target) return;
          if (target.id === "pi-tab") select(true);
          else if (active) { active = false; panel.classList.add("hidden"); }
        });
        if (view) render(panel); else refresh();
      }
    } else if (!standalone) {
      const wrap = root.parentElement || document.body;
      const card = document.createElement("div");
      card.id = "pi-standalone";
      wrap.appendChild(card);
      if (view) render(card); else refresh();
    }
  }
  new MutationObserver(mount).observe(root, { childList: true });
  renderBanner();
  refreshBanner();   // mounts the Person Images tab once the document type is known
})();
