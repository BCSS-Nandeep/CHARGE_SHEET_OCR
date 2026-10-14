/* Structured tab: renders results.structured from the API response.
 *
 * Nothing about the fields is hardcoded here: the sections, their order, the field labels and
 * whether a section repeats (accused, witnesses, ...) all come from `structured_layout` in the
 * response, and the values from `results.structured`. Edits use the same inputs / Save button as
 * the Fields tab (class "edit-input", data-field-id = "s.<section>.<field>" or
 * "s.<section>.<n>.<field>"), so a correction is stored beside the machine value, never over it.
 * Relies on helpers from app.js: escapeHtml, badge, authHeaders.
 */

(function () {
  const STATUS_HELP = {
    extracted: "Found verbatim in the page text under a fitting label",
    not_found: "No page states this field (nothing was guessed)",
    explicit_na: "The document itself says N/A / nil / not applicable",
    illegible: "The value contains an unreadable-text marker",
    ambiguous: "Value present, but the label next to it does not fit this field",
    conflicting: "Different pages give different values — all are listed; choose or enter the right one",
    unverified: "The model returned this value but it was not found in the page text",
  };

  const isOpen = (v) => v && v.needs_review && !(v.corrected_value != null && v.corrected_value !== "");
  const shown = (v) => (v.corrected_value != null && v.corrected_value !== "" ? String(v.corrected_value) : v.value == null ? "" : String(v.value));
  const pageLabel = (v) => {
    const pages = v.pages && v.pages.length ? v.pages : v.source_page != null ? [v.source_page] : [];
    return pages;
  };

  function statusBadge(v) {
    const human = v.corrected_value != null && v.corrected_value !== "";
    const machine = `<span class="badge st-${escapeHtml(v.status)}" title="${escapeHtml(STATUS_HELP[v.status] || "")}">${escapeHtml(v.status.replace(/_/g, " "))}</span>`;
    return human ? `${machine} <span class="badge corrected" title="A human entered or changed this value; the machine value is kept below">human-corrected</span>` : machine;
  }

  function pageLinks(v) {
    const pages = pageLabel(v);
    if (!pages.length) return `<span class="muted">—</span>`;
    return pages.map((p) => `<a href="#" class="page-link" data-page="${escapeHtml(p)}" title="Show page ${escapeHtml(p)}">p.${escapeHtml(p)}</a>`).join(" ");
  }

  function candidatesBlock(v) {
    if (!v.candidates || v.candidates.length < 2 || v.status !== "conflicting") return "";
    const items = v.candidates.map((c) => `
      <li>
        <a href="#" class="page-link" data-page="${escapeHtml(c.source_page)}">p.${escapeHtml(c.source_page)}</a>
        <strong>${escapeHtml(c.value)}</strong>
        <button type="button" class="btn secondary tiny" data-use-value="${escapeHtml(c.value)}">use this</button>
        ${c.source_text ? `<div class="source-snippet">${escapeHtml(c.source_text)}</div>` : ""}
      </li>`).join("");
    return `<ul class="candidates">${items}</ul>`;
  }

  /* Values the machine saw but did not use (no evidence under a fitting label): shown, never hidden */
  function otherSeen(v) {
    if (v.status === "conflicting" || !v.candidates || !v.candidates.length) return "";
    const used = String(v.value == null ? "" : v.value).replace(/[^a-z0-9]/gi, "").toLowerCase();
    const rest = v.candidates.filter((c) => String(c.value).replace(/[^a-z0-9]/gi, "").toLowerCase() !== used);
    if (!rest.length) return "";
    const items = rest.map((c) => `<li><a href="#" class="page-link" data-page="${escapeHtml(c.source_page)}">p.${escapeHtml(c.source_page)}</a>
      <strong>${escapeHtml(c.value)}</strong> ${badge(c.status)}
      <button type="button" class="btn secondary tiny" data-use-value="${escapeHtml(c.value)}">use this</button></li>`).join("");
    return `<details><summary class="small">${rest.length} other value(s) seen but not used</summary><ul class="candidates">${items}</ul></details>`;
  }

  function valueRow(path, label, v) {
    const value = shown(v);
    const corrected = v.corrected_value != null && v.corrected_value !== "";
    const multi = value.length > 70 || value.includes("\n");
    const rows = Math.min(10, Math.max(2, Math.ceil(value.length / 60)));
    const id = escapeHtml("s." + path);
    const placeholder = v.status === "not_found" ? "(not found — enter if the document has it)" : v.status === "conflicting" ? "(conflicting — choose or enter)" : "";
    const input = multi
      ? `<textarea class="edit-input value-input" rows="${rows}" data-field-id="${id}" data-initial="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}">${escapeHtml(value)}</textarea>`
      : `<input type="text" class="edit-input value-input" data-field-id="${id}" data-initial="${escapeHtml(value)}" value="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}" />`;
    const machine = corrected
      ? `<details><summary class="small">machine value (before correction)</summary><div class="source-snippet">${v.value == null ? "<em>none</em>" : escapeHtml(v.value)}</div></details>` : "";
    const source = v.source_text
      ? `<details><summary class="small">source text</summary><div class="source-snippet">${escapeHtml(v.source_text)}</div></details>` : "";
    const classes = ["s-row", `st-${v.status}`];
    if (isOpen(v)) classes.push("needs-review");
    return `<tr class="${classes.join(" ")}" data-path="${escapeHtml(path)}" data-search="${escapeHtml((label + " " + value + " " + v.status).toLowerCase())}">
      <td class="label-cell">${escapeHtml(label)}</td>
      <td>${input}${machine}${candidatesBlock(v)}${otherSeen(v)}${v.note ? `<div class="muted small">${escapeHtml(v.note)}</div>` : ""}</td>
      <td>${statusBadge(v)}${isOpen(v) ? ` <span class="badge needs_review" title="A reviewer should check this value">review</span>` : ""}</td>
      <td class="src-cell">${pageLinks(v)}${source}</td>
    </tr>`;
  }

  const TABLE_HEAD = `<thead><tr><th>Field</th><th>Value</th><th>Status</th><th>Source</th></tr></thead>`;

  function singleSection(sec, fields) {
    const rows = sec.fields.map((f) => valueRow(`${sec.key}.${f.key}`, f.label, fields[f.key] || { status: "not_found" })).join("");
    const open = sec.fields.filter((f) => isOpen(fields[f.key])).length;
    return `<details class="card section struct-section" open data-section="${escapeHtml(sec.key)}">
      <summary><h3>${escapeHtml(sec.label)}${open ? ` <span class="badge needs_review">${open} to review</span>` : ""}</h3></summary>
      <table class="field-table struct-table">${TABLE_HEAD}<tbody>${rows}</tbody></table>
    </details>`;
  }

  function recordTitle(sec, rec, i) {
    const pick = (k) => (rec.fields[k] ? shown(rec.fields[k]) : "");
    const main = pick("name") || pick("description") || [pick("act"), pick("section")].filter(Boolean).join(" ");
    const serial = pick("serial_no");
    return `${serial ? escapeHtml(serial) + ". " : ""}${main ? escapeHtml(main) : `<span class="muted">${escapeHtml(sec.label)} ${i + 1} (no name found)</span>`}`;
  }

  function repeatedSection(sec, records) {
    if (!records.length) {
      return `<details class="card section struct-section" open data-section="${escapeHtml(sec.key)}">
        <summary><h3>${escapeHtml(sec.label)} <span class="badge missing">none found</span></h3></summary>
        <p class="muted">No ${escapeHtml(sec.label.toLowerCase())} were found on any page (nothing was invented).</p>
      </details>`;
    }
    const cards = records.map((rec, i) => {
      const open = sec.fields.filter((f) => isOpen(rec.fields[f.key])).length;
      const pages = (rec.pages || []).map((p) => `<a href="#" class="page-link" data-page="${escapeHtml(p)}">p.${escapeHtml(p)}</a>`).join(" ");
      const rows = sec.fields.map((f) => valueRow(`${sec.key}.${i}.${f.key}`, f.label, rec.fields[f.key] || { status: "not_found" })).join("");
      return `<details class="person-card struct-record" data-record="${escapeHtml(sec.key + "." + i)}"${records.length <= 6 || open ? " open" : ""}>
        <summary><strong>${recordTitle(sec, rec, i)}</strong>
          <span class="muted small">${pages}</span>
          ${open ? `<span class="badge needs_review">${open} to review</span>` : ""}</summary>
        ${rec.note ? `<div class="muted small">${escapeHtml(rec.note)}</div>` : ""}
        <table class="field-table struct-table">${TABLE_HEAD}<tbody>${rows}</tbody></table>
      </details>`;
    }).join("");
    return `<details class="card section struct-section" open data-section="${escapeHtml(sec.key)}">
      <summary><h3>${escapeHtml(sec.label)} <span class="muted small">(${records.length})</span></h3></summary>${cards}
    </details>`;
  }

  function otherDetails(list) {
    if (!list || !list.length) return "";
    const rows = list.map((d) => `<tr class="s-row" data-search="${escapeHtml((d.label + " " + d.value).toLowerCase())}">
      <td class="label-cell">${escapeHtml(d.label)}</td><td>${escapeHtml(d.value)}</td>
      <td>${badge(d.status)}</td><td>${d.source_page != null ? `<a href="#" class="page-link" data-page="${escapeHtml(d.source_page)}">p.${escapeHtml(d.source_page)}</a>` : "—"}
      ${d.source_text ? `<details><summary class="small">source text</summary><div class="source-snippet">${escapeHtml(d.source_text)}</div></details>` : ""}</td></tr>`).join("");
    return `<details class="card section struct-section">
      <summary><h3>Other details found <span class="muted small">(${list.length}) — fit no field; kept for review</span></h3></summary>
      <table class="field-table struct-table">${TABLE_HEAD}<tbody>${rows}</tbody></table></details>`;
  }

  function summaryBar(s, counts) {
    const chip = (n, cls, text) => (n ? `<span class="chip ${cls}"><strong>${n}</strong> ${text}</span>` : "");
    const status = s.document_status;
    const banner = status === "failed"
      ? `<div class="alert error"><strong>Structured mapping failed.</strong> No page could be mapped to the fields, so every value below is empty — nothing was invented. The as-printed fields are in the Fields tab.</div>`
      : status === "partial"
        ? `<div class="alert warn"><strong>Partly mapped.</strong> Some pages could not be read or mapped (see the page table); fields that appear only on those pages are shown as not found.</div>` : "";
    const warn = (s.warnings || []).map((w) => `<li>${escapeHtml(w)}</li>`).join("");
    return `${banner}
      <div class="chips">
        ${chip(counts.needs_review, "c-review", "to review")}
        ${chip(counts.conflicting, "c-conflict", "conflicting")}
        ${chip(counts.ambiguous, "c-review", "ambiguous")}
        ${chip(counts.unverified, "c-review", "unverified")}
        ${chip(counts.illegible, "c-review", "illegible")}
        ${chip(counts.not_found, "c-missing", "not found")}
        ${chip(counts.explicit_na, "c-ok", "stated N/A")}
        ${chip(counts.extracted, "c-ok", "extracted")}
      </div>
      <p class="muted small">Machine-extracted values are <strong>not verified</strong>. "Extracted" means the value was found in the page text; a reviewer must still check it against the document.</p>
      ${warn ? `<ul class="warnings">${warn}</ul>` : ""}`;
  }

  function validationBlock(v) {
    const issues = (v && v.issues) || [];
    if (!issues.length) return `<p class="muted small">Validation: no issues.</p>`;
    const rows = issues.map((i) => `<tr><td>${badge(i.severity === "error" ? "invalid" : i.severity === "info" ? "pending" : "uncertain")}</td>
      <td class="mono small">${escapeHtml(i.code)}</td>
      <td>${i.path ? `<a href="#" class="path-link mono small" data-path="${escapeHtml(i.path)}">${escapeHtml(i.path)}</a>` : ""}${i.page != null ? ` <a href="#" class="page-link" data-page="${escapeHtml(i.page)}">p.${escapeHtml(i.page)}</a>` : ""}</td>
      <td>${escapeHtml(i.message)}</td></tr>`).join("");
    const errors = issues.filter((i) => i.severity === "error").length;
    return `<details><summary>Validation: ${issues.length} issue(s)${errors ? ` — <span class="warn-text">${errors} error(s)</span>` : ""}${v.is_valid ? "" : " (result not consistent)"}</summary>
      <table class="field-table"><thead><tr><th></th><th>Check</th><th>Where</th><th>Detail</th></tr></thead><tbody>${rows}</tbody></table></details>`;
  }

  function pagesBlock(pages) {
    if (!pages || !pages.length) return "";
    const rows = pages.map((p) => `<tr><td><a href="#" class="page-link" data-page="${escapeHtml(p.page)}">${escapeHtml(p.page)}</a></td>
      <td>${escapeHtml(p.text_engine)}</td><td>${badge(p.text_status)}</td><td>${badge(p.mapping_status)}</td>
      <td>${p.transcribe_seconds != null ? escapeHtml(p.transcribe_seconds) + " s" : "—"}</td>
      <td>${p.map_seconds != null ? escapeHtml(p.map_seconds) + " s" : "—"}</td><td>${escapeHtml(p.note || "")}</td></tr>`).join("");
    return `<details><summary>How each page was read and mapped</summary>
      <table class="field-table"><thead><tr><th>Page</th><th>Text from</th><th>Text</th><th>Mapping</th><th>Read</th><th>Map</th><th>Note</th></tr></thead><tbody>${rows}</tbody></table></details>`;
  }

  function narrativeBlock(ids, fields) {
    const byId = new Map((fields || []).map((f) => [f.id, f]));
    const items = (ids || []).map((id) => byId.get(id)).filter(Boolean);
    if (!items.length) return "";
    const list = items.map((f) => `<li><strong>${escapeHtml(f.label || "Running text")}</strong> <span class="muted small">p.${escapeHtml(f.page)}</span>
      <div class="source-snippet">${escapeHtml(shownText(f).slice(0, 300))}${shownText(f).length > 300 ? "…" : ""}</div></li>`).join("");
    return `<details class="card section struct-section"><summary><h3>Narrative / brief facts <span class="muted small">— kept as printed; edit in the Fields tab</span></h3></summary><ul class="conflicts">${list}</ul></details>`;
  }
  const shownText = (f) => (f.corrected_value != null && f.corrected_value !== "" ? String(f.corrected_value) : String(f.value || ""));

  /* ---------- page preview ---------- */

  function previewPane(host, ctx, state) {
    const total = Math.max(1, Number(ctx.pageCount) || 1);
    const pane = host.querySelector(".doc-preview");
    const img = pane.querySelector("img");
    const sel = pane.querySelector("select");
    const msg = pane.querySelector(".preview-msg");
    const cache = ctx.pageCache || (ctx.pageCache = new Map());
    let token = 0;

    async function show(n) {
      n = Math.min(total, Math.max(1, Number(n) || 1));
      state.page = n;
      sel.value = String(n);
      const mine = ++token;
      msg.textContent = "Loading page…";
      try {
        if (!cache.has(n)) {
          const res = await fetch(`${ctx.docUrl}/pages/${n}/image?width=1000`, { headers: authHeaders() });
          if (!res.ok) throw new Error(res.status === 404 ? "Page not available" : `Preview unavailable (${res.status})`);
          cache.set(n, URL.createObjectURL(await res.blob()));
        }
        if (mine !== token) return;
        img.src = cache.get(n);
        img.alt = `Page ${n} of the document`;
        msg.textContent = "";
      } catch (err) {
        if (mine === token) { img.removeAttribute("src"); msg.textContent = err.message; }
      }
    }

    pane.querySelector(".prev").onclick = () => show(state.page - 1);
    pane.querySelector(".next").onclick = () => show(state.page + 1);
    sel.onchange = () => show(sel.value);
    return show;
  }

  /* ---------- main ---------- */

  function render(host, ctx) {
    const s = ctx.structured;
    const layout = ctx.layout || [];
    const state = ctx.state;
    const tally = { needs_review: 0, extracted: 0, not_found: 0, explicit_na: 0, illegible: 0, ambiguous: 0, conflicting: 0, unverified: 0 };
    const visit = (v) => { tally[v.status] = (tally[v.status] || 0) + 1; if (isOpen(v)) tally.needs_review += 1; };
    Object.values(s.single || {}).forEach((f) => Object.values(f).forEach(visit));
    Object.values(s.lists || {}).forEach((recs) => recs.forEach((r) => Object.values(r.fields).forEach(visit)));

    const sections = layout.map((sec) => (sec.repeated
      ? repeatedSection(sec, (s.lists || {})[sec.key] || [])
      : singleSection(sec, (s.single || {})[sec.key] || {}))).join("");
    const pageOptions = Array.from({ length: Math.max(1, Number(ctx.pageCount) || 1) }, (_, i) => `<option value="${i + 1}">Page ${i + 1}</option>`).join("");

    host.innerHTML = `
      <div class="struct-layout">
        <aside class="doc-preview">
          <div class="preview-bar">
            <button type="button" class="btn secondary tiny prev" aria-label="Previous page">◀</button>
            <select aria-label="Page">${pageOptions}</select>
            <button type="button" class="btn secondary tiny next" aria-label="Next page">▶</button>
          </div>
          <div class="preview-msg muted small"></div>
          <img alt="" />
        </aside>
        <section class="struct-main">
          <div class="card">
            <h2>Structured fields <span class="muted small">schema ${escapeHtml(s.schema_version)} · mapped by ${escapeHtml(s.mapping_model || "—")} · text from ${escapeHtml(s.transcribe_engine || "—")}</span></h2>
            ${summaryBar(s, tally)}
            ${validationBlock(s.validation)}
            ${pagesBlock(s.pages)}
            <div class="row" style="margin-top:0.75rem">
              <input id="struct-filter" type="search" placeholder="Filter by field, value or status…" value="${escapeHtml(state.filter || "")}" />
              <label class="small"><input type="checkbox" id="struct-only-review"${state.onlyReview ? " checked" : ""} /> only items to review</label>
              <button type="button" class="btn secondary tiny" id="struct-download" title="The final structured JSON, including saved corrections">Download final JSON</button>
            </div>
          </div>
          ${sections}
          ${otherDetails(s.other_details)}
          ${narrativeBlock(s.narrative_field_ids, ctx.fields)}
        </section>
      </div>`;

    const show = previewPane(host, ctx, state);
    show(state.page || 1);

    function applyFilter() {
      const q = (document.getElementById("struct-filter").value || "").trim().toLowerCase();
      const only = document.getElementById("struct-only-review").checked;
      state.filter = q;
      state.onlyReview = only;
      host.querySelectorAll("tr.s-row").forEach((tr) => {
        const hide = (q && !tr.dataset.search.includes(q)) || (only && !tr.classList.contains("needs-review"));
        tr.classList.toggle("hidden", Boolean(hide));
      });
      host.querySelectorAll("details.struct-record").forEach((d) => {
        const visible = d.querySelectorAll("tr.s-row:not(.hidden)").length > 0;
        d.classList.toggle("hidden", !visible);
        if ((q || only) && visible) d.open = true;
      });
    }
    document.getElementById("struct-download").addEventListener("click", async () => {
      try {
        const res = await fetch(`${ctx.docUrl}/structured?download=true`, { headers: authHeaders() });
        if (!res.ok) throw new Error(`Download failed (${res.status})`);
        const url = URL.createObjectURL(await res.blob());
        const a = document.createElement("a");
        a.href = url;
        a.download = "structured.json";
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
      } catch (err) {
        alert(err.message);
      }
    });
    document.getElementById("struct-filter").addEventListener("input", applyFilter);
    document.getElementById("struct-only-review").addEventListener("change", applyFilter);
    if (state.filter || state.onlyReview) applyFilter();

    host.addEventListener("click", (e) => {
      const pageLink = e.target.closest("a.page-link");
      if (pageLink) {
        e.preventDefault();
        show(pageLink.dataset.page);
        host.querySelector(".doc-preview").scrollIntoView({ block: "nearest", behavior: "smooth" });
        return;
      }
      const pathLink = e.target.closest("a.path-link");
      if (pathLink) {
        e.preventDefault();
        const row = host.querySelector(`tr[data-path="${CSS.escape(pathLink.dataset.path)}"]`);
        if (row) {
          row.classList.remove("hidden");
          row.closest("details")?.setAttribute("open", "");
          row.closest("details.struct-section")?.setAttribute("open", "");
          row.scrollIntoView({ block: "center", behavior: "smooth" });
          row.classList.add("flash");
          setTimeout(() => row.classList.remove("flash"), 1600);
        }
        return;
      }
      const use = e.target.closest("[data-use-value]");
      if (use) {
        const input = use.closest("tr").querySelector(".edit-input");
        input.value = use.dataset.useValue;
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
    });
    // a field with a source page shows that page when the reviewer starts editing it
    host.addEventListener("focusin", (e) => {
      const input = e.target.closest(".edit-input");
      if (!input) return;
      const link = input.closest("tr").querySelector("a.page-link");
      if (link && Number(link.dataset.page) !== state.page) show(link.dataset.page);
    });
    host.addEventListener("input", (e) => {
      const input = e.target.closest(".edit-input");
      if (input) input.closest("tr").classList.toggle("dirty", input.value !== input.dataset.initial);
    });
  }

  window.StructuredView = { render };
})();
