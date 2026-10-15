/* Person Images tab for the IR_OCR results page.
 *
 * Added by the person-images integration (the OCR UI files are not edited): the integrated app's middleware adds this
 * script to /results/{id}. It waits for the existing results view to render, adds one more tab to its tab bar and shows
 * the extracted person images. If OCR produced no tab bar (for example OCR failed), the images are still shown in
 * their own card. It only calls the new person-image API and never touches the OCR review controls.
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

  let view = null;          // last status from the API
  let timer = null;
  let blobUrls = [];
  let active = false;       // our tab is the selected one

  function key() {
    try { return sessionStorage.getItem("ir_api_key") || ""; } catch (e) { return ""; }
  }
  function headers() {
    const h = {};
    const k = key();
    if (k) h[HEADER] = k;
    return h;
  }
  function esc(s) {
    return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
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

  const STATUS_TEXT = {
    not_started: "Image extraction has not started for this document.",
    queued: "Image extraction is queued…",
    processing: "Extracting person images…",
    succeeded: "",
    partial: "Some images in this document could not be read; the person images found are shown.",
    no_person_images: "No person images were found in this document.",
    failed: "Image extraction failed. OCR results are not affected.",
    unsupported: "This file type is not supported by the image extraction.",
  };

  function badge(status) {
    const s = String(status || "unknown").toLowerCase().replace(/[^a-z_]/g, "");
    const cls = s === "succeeded" ? "completed" : s === "failed" ? "failed" : s === "partial" ? "partial" : s;
    return `<span class="badge ${cls}">${esc(s.replace(/_/g, " "))}</span>`;
  }

  function releaseBlobs() {
    blobUrls.forEach((u) => URL.revokeObjectURL(u));
    blobUrls = [];
  }

  /* Images need the API key header, which an <img src> cannot send: fetch them and show a blob URL. */
  async function loadImage(img, url) {
    try {
      const res = await fetch(url, { headers: headers() });
      if (!res.ok) throw new Error(String(res.status));
      const u = URL.createObjectURL(await res.blob());
      blobUrls.push(u);
      img.src = u;
    } catch (e) {
      img.replaceWith(Object.assign(document.createElement("div"), {
        className: "pi-missing", textContent: "Image file not available",
      }));
    }
  }

  function render(host) {
    releaseBlobs();
    const v = view || { status: "not_started", images: [] };
    const images = v.images || [];
    const note = v.status === "failed" && v.error ? `${STATUS_TEXT.failed} (${esc(v.error)})` : (STATUS_TEXT[v.status] || "");
    const tone = v.status === "failed" ? "error" : (v.status === "partial" ? "warn" : "info");
    const stats = images.length || v.total_images_found
      ? `<span class="muted small">${images.length} person image${images.length === 1 ? "" : "s"} · ${v.total_images_found || 0} image(s) found in the document · ${v.skipped_images || 0} skipped as not showing a person${v.unreadable_images ? ` · ${v.unreadable_images} unreadable` : ""}</span>`
      : "";
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
              ${im.available && im.url ? `<img alt="Person image ${i + 1}" data-url="${esc(im.url)}" />`
                : `<div class="pi-missing">Image file not available</div>`}
              <figcaption>
                <strong>${esc(im.name)}</strong>
                <span class="muted small">${im.source_page ? `page ${esc(im.source_page)}` : esc(im.source_member || "embedded image")} · ${esc(im.width)}×${esc(im.height)} px · ${esc(im.faces)} face${im.faces === 1 ? "" : "s"}</span>
              </figcaption>
            </figure>`).join("")}
        </div>
      </div>`;
    host.querySelectorAll("img[data-url]").forEach((img) => loadImage(img, img.dataset.url));
    const msg = host.querySelector("#pi-msg");
    const flash = (text, kind) => { msg.className = `alert ${kind}`; msg.textContent = text; };
    host.querySelector("#pi-rerun").onclick = async () => {
      try {
        await getJson(`${API}/person-images/run`, { method: "POST" });
        flash("Image extraction started.", "info");
        await refresh();
      } catch (e) { flash(e.message, "error"); }
    };
    host.querySelector("#pi-report").onclick = async () => {
      try {
        const data = await getJson(`${API}/report_result`);
        const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
        const a = Object.assign(document.createElement("a"), { href: url, download: "report_result.json" });
        document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
      } catch (e) { flash(e.message, "error"); }
    };
  }

  function host() { return document.getElementById("pi-panel") || document.getElementById("pi-standalone"); }

  async function refresh() {
    try {
      view = await getJson(`${API}/person-images`);
    } catch (e) {
      view = { status: "failed", error: e.message, images: [] };
    }
    const h = host();
    if (h) render(h);
    clearTimeout(timer);
    if (view && (view.status === "queued" || view.status === "processing")) timer = setTimeout(refresh, 3000);
  }

  function select(on) {
    active = on;
    const panel = document.getElementById("pi-panel");
    const tab = document.getElementById("pi-tab");
    if (!panel || !tab) return;
    panel.classList.toggle("hidden", !on);
    if (on) {
      ["tab-fields", "tab-raw"].forEach((id) => { const el = document.getElementById(id); if (el) el.classList.add("hidden"); });
      root.querySelectorAll(".tab").forEach((b) => { b.classList.toggle("active", b === tab); b.setAttribute("aria-selected", String(b === tab)); });
      refresh();
    }
  }

  /* Add the tab to the OCR tab bar (or show a standalone card when there is none). Safe to call repeatedly. */
  function mount() {
    const tabs = root.querySelector(".tabs");
    const standalone = document.getElementById("pi-standalone");
    if (tabs) {
      if (standalone) standalone.remove();
      if (!document.getElementById("pi-tab")) {
        const btn = document.createElement("button");
        btn.className = "tab";
        btn.id = "pi-tab";
        btn.type = "button";
        btn.setAttribute("role", "tab");
        btn.setAttribute("aria-selected", "false");
        btn.textContent = "Person Images";
        tabs.appendChild(btn);
        const panel = document.createElement("div");
        panel.id = "pi-panel";
        panel.className = "hidden";
        tabs.insertAdjacentElement("afterend", panel);
        tabs.addEventListener("click", (e) => {
          const target = e.target.closest(".tab");
          if (!target) return;
          if (target.id === "pi-tab") select(true);
          else if (active) { active = false; panel.classList.add("hidden"); }
        });
        if (view) render(panel);
        else refresh();
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
  mount();
})();
