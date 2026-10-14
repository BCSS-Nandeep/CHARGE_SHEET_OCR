# Charge Sheet PDF Extraction Service

Standalone **local-only** FastAPI service with an HTML UI for uploading charge-sheet PDFs, extracting text
via a **hybrid page router** (PyMuPDF native text → PaddleOCR + PP-Structure for scans → optional Docling
assist on digital forms → Tesseract fallback → optional local PaddleOCR-VL for hard pages), detecting the
**labels and values printed in each PDF** (no fixed field list, no per-template parsers), and exposing the
result over a REST API and an editable review UI.
> **Security:** designed for confidential police data. No document content leaves the host. See
> [docs/SECURITY.md](docs/SECURITY.md). Audit findings and design notes: [docs/AUDIT_REPORT.md](docs/AUDIT_REPORT.md).

## Features

- Digital and scanned multi-page PDFs (UI + API); OCR runs locally, per page, with honest failure reporting
- Layout-aware parsing: ruled form tables, OCR'd tables rebuilt from text-box geometry, header tables
  (label row over value row), `Label: value` / space-aligned text layouts, glued (space-less) PDF exports
- **The PDF decides the fields** (schema 2.0.0): the result is one list of fields in reading order, each
  with the label exactly as printed (header-table label, form-row label cell, text before a colon, or a
  heading line), the value exactly as printed (lines kept), the page(s), the form row number if printed,
  and OCR confidence for scanned pages. A field that is not in the PDF is not in the result
- Continuity: everything after a label (beside it, below it, on the following pages) is its value until the
  next label; rows under a list heading (witnesses, accused) stay in that heading's value; the running
  narrative is one value with paragraphs kept; text before the first label is kept without a label
- The built-in label vocabulary is used only to *recognise* that a phrase is a label — labels are never
  renamed. Scanner watermarks ("Scanned with CamScanner") and header/footer lines repeated on several
  pages are left out; missing spaces in glued PDF exports are restored
- **JSON tab** (results page): the final JSON of the chargesheet, built from the same fields as the **Fields**
  tab — one key per field, named exactly as printed in *that* document (a different template gives different
  keys), the value the complete final text (long multi-line fields such as the accused or witnesses list, with
  their arrest / remand notes, stay one key), saved corrections included, missing values `""`. Copy / Download
  buttons; `GET /api/v1/documents/{id}/json` returns the same JSON. No fixed schema
- Optional local vision model (see the section below): reads scanned pages and builds a canonical structured
  result kept internally (`/structured` endpoint) — it has no tab of its own
- PDF, PNG, JPEG, TIFF and DOCX uploads (originals kept unchanged)
- Raw lines (`/raw` endpoint, no tab): every line read from each page exactly as read (text layer, OCR or vision
  model), top to bottom — kept for debugging and audit; stored locally next to the results and deleted with the document
- Separate statuses: extraction (`complete` / `partial` / `failed`) and review (`unreviewed`, `corrected`,
  `verified`). Results from schema 1.x load as "reprocess required"- Corrections stored separately with the extracted value they replaced, the reviewer and an audit trail;
  re-applied after reprocessing only when the source value is unchanged
- Role-based API keys; verification requires an authenticated reviewer and notes
- Retention purge and on-demand deletion remove the PDF, extracted data and corrections

## Quick start (Windows PowerShell)

```powershell
cd C:\Users\Dell\Desktop\CHARGE_SHEET_OCR
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then edit .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- UI: http://127.0.0.1:8000/  ·  History: http://127.0.0.1:8000/history
- API docs: http://127.0.0.1:8000/docs (disable with `API_DOCS_ENABLED=false` on air-gapped networks —
  the Swagger page loads its scripts from cdn.jsdelivr.net in the browser)

### Authentication

| Mode | Behaviour |
|---|---|
| `ENABLE_AUTH=false` (default) | Only loopback clients (127.0.0.1) are served, as an unauthenticated *operator*: upload, view, correct, reprocess. **Results cannot be marked verified.** |
| `ENABLE_AUTH=true` | Every API call needs a key in the `API_KEY_HEADER` header (default `X-API-Key`). |

Keys: `API_KEY` (user `default`, role admin) and/or `API_KEYS=name:key:role;...` with roles
`viewer < operator < reviewer < admin`. Keys must be at least 16 characters; the example values
(`change-me-to-a-long-random-secret`, `dev-local-api-key-change-me`) are refused. Generate one with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`. In the UI, paste the key into the
*API key* box (kept for the browser tab only).

### Local OCR for scanned PDFs

Scanned PDFs need a local OCR engine; without one they are reported as **failed** (`OCR_REQUIRED`),
never as an empty successful extraction. `GET /health` shows `ocr_available`, `ocr_engine` and any engine error.

- **PaddleOCR (preferred):** `pip install paddlepaddle==3.3.1 paddleocr==3.7.0`, then run one OCR job on a
  controlled network so the PP-OCR models are cached under `%USERPROFILE%\.paddlex\official_models`.
  At runtime downloads and model-hoster probes are disabled/blocked. Keep `OCR_ENABLE_MKLDNN=false`: Paddle
  3.x's oneDNN kernels fail on this CPU (`ConvertPirAttribute2RuntimeAttribute not support`).
  CPU speed is roughly one minute per page at 200 DPI.
- **Tesseract (fallback):** install the Tesseract binary *and* `pip install pytesseract`; set `TESSERACT_CMD`
  if `tesseract.exe` is not on PATH. The Python wrapper alone is not an OCR engine.

## Structured extraction with a local vision-language model

The as-printed field list above is unchanged. With `VLM_ENABLED=true` the service also builds a **structured**
result (`results.structured`, schema `1.0`): the same sections and field names for every document, whatever
its layout — case details, complainant, informant, acts and sections, investigating officers, accused,
witnesses, property — each value with its status, source page and source text. It is stored with the results
and served by `/api/v1/documents/{id}/structured`; the results page does not show it (its tabs are **Fields** and
**JSON**, which are built from the as-printed fields).

### Setup

Configure the model server in `.env` (see `.env.example`; nothing is hardcoded and there is no default URL):

| Key | Meaning |
|---|---|
| `VLM_ENABLED` | `true` turns the structured pipeline on |
| `VLM_BASE_URL`, `VLM_MODEL`, `VLM_API_KEY` | OpenAI-compatible server, model name, optional bearer token (never logged) |
| `VLM_TRANSCRIBE_ENGINE` | `nanonets` (default): the model reads scanned / garbled page **images**. `paddleocr`: PaddleOCR reads them (about 35 s/page) and the model only maps fields — use it to compare speed and quality |
| `VLM_FALLBACK_ENGINE` | `none` (default): a page the model cannot read is reported **failed**. `paddleocr`: that page is read by PaddleOCR instead and the page summary says so. Never switched silently |
| `VLM_TIMEOUT_SECONDS`, `VLM_MAX_RETRIES`, `VLM_RETRY_BACKOFF_SECONDS` | per-request timeout; retries only for timeouts, connection errors and 5xx |
| `VLM_ABORT_AFTER_FAILURES`, `VLM_DOCUMENT_BUDGET_SECONDS` | stop asking an unresponsive server / cap the time per document (remaining pages are skipped, result is *partial*) |
| `VLM_MAX_IMAGE_SIDE`, `VLM_*_MAX_TOKENS`, `VLM_MAP_CHUNK_CHARS` | page image size and output / input limits |
| `VLM_WAIT_IDLE_SECONDS` | wait (read-only `/slots` check) for a shared server to be idle before each request |

`GET /health` reports `vlm_enabled`, `vlm_available` (server reachable and the model served), `vlm_model`.

**The server handles one request at a time and may be shared with other applications.** This service sends
one request at a time (process-wide lock) and does not change the server's configuration. On a CPU-only
llama.cpp server (measured 2026-10-14 on the synthetic fixtures) a sparse scanned page took **19-75 s** to read
(the slower figure while the server was busy with another application) and each page's mapping request
**24-65 s**; a 3-page digital document took about 2.5 minutes. Dense real pages take longer, so a long scanned
document can run for an hour; it runs in the background with per-page progress on the results page.
The mapping request asks the model only about the sections a page shows signs of (heading / label vocabulary),
which is what keeps it short and limits invented values; `VLM_MAP_MAX_TOKENS` (1500) stops a runaway answer.
The model fills every field it is shown (with "N/A" filler that is discarded), so on a **dense page** (more than
`VLM_MAP_SPLIT_CHARS` = 1800 characters) each section is asked in its own request, and a combined answer that is
cut off at the token limit is retried one section at a time. A section that still cannot be mapped makes the page
*partial*; only a page where every request failed is *failed*. (On the first run over real documents, dense pages
were cut off at the cap and whole documents came back partial or failed — this is the fix.)
These timings are from one machine and a small sample, not a benchmark.

### How a page becomes structured fields

1. **Text.** Digital pages use the PDF text layer (laid out in visual rows, cells joined with `|`). Scanned or
   garbled pages go to the model as an image (or to PaddleOCR, see `VLM_TRANSCRIBE_ENGINE`); the model's
   output (text, HTML tables) is kept as read in the raw lines (`/raw`, `source: "vlm"`) and also feeds the as-printed
   field builder. PaddleOCR is never run on a page the model already read.
2. **Mapping.** Every readable page is sent, one request each, with a JSON-schema-constrained prompt; the
   model returns **values only** (copied as printed, omitted when the page does not state them).
3. **Grounding.** Python looks for each returned value in that page's text. The source page and source
   text come from the matching line — never from the model. A value that is not on the page is kept and
   marked `unverified`. An N/A counts as the document's answer only when a label for *that field* is next to
   it; FIR number / crime number / dates / charge-sheet number need their own label near the value, else
   `ambiguous` (`FIR No.` and `Crime No.` are never treated as interchangeable).
4. **Merge.** Equal values from several pages become one value listing every page; different values become
   `conflicting` with **every candidate kept** (nothing is overwritten or auto-resolved). A person on several
   pages (same name) is one record; two people are never merged, and a name seen twice on one page is
   flagged as a duplicate. Information that fits no field goes to *other details*.
5. **Validation** (`app/services/structured_validation.py`, separate from mapping): schema shape, unexpected
   keys, source-page references, value ⊂ source text ⊂ cited page, date / year / identifier / section
   formats, duplicates, conflicts, expected fields missing (police station, FIR or crime number, accused,
   acts). It reports; it never rewrites a value (a date that does not parse stays as printed and is flagged).

### Output conventions

Every value is `{value, status, source_page, source_text, pages, candidates, text_engine, needs_review,
note, corrected_value, review_status, review_notes}`. Statuses (the only ones produced):

| status | meaning |
|---|---|
| `extracted` | found verbatim in the page text under a fitting label |
| `not_found` | no page states it (always present, never guessed) |
| `explicit_na` | the document itself says N/A / nil / not applicable |
| `illegible` | the value contains an unreadable-text marker |
| `ambiguous` | present, but the label next to it does not fit this field |
| `conflicting` | different pages disagree; `value` is `null`, `candidates` lists all |
| `unverified` | the model returned it but it is not in the page text |

`needs_review` is true for everything except `extracted` / `explicit_na` / `not_found`, and for any value from a
page read with reduced quality. **No status means "verified"** — only a reviewer verifies a document, with
notes. A human correction is stored in `corrected_value` beside the machine `value` (and in the correction
history with the value it replaced); it survives a reprocess only if the machine value is unchanged.
Repeated entities are arrays in `lists`; `structured_layout` in the results response lists sections, labels and
order, which is all the UI uses to draw a result.

### Image and DOCX uploads

PNG, JPEG and TIFF (multi-frame) are accepted besides PDF; the type is decided from the content. The upload is stored
unchanged. A one-page-per-image PDF (`<stored name>.pages.pdf`) is derived from it (EXIF orientation applied,
other colour modes converted to RGB, images larger than 5000 px scaled down) and processed like any PDF.
DOCX: paragraphs, tables (cell order kept) and embedded pictures are laid out **in reading order** into a derived
PDF by PyMuPDF. **Page numbers for a DOCX refer to that rendition, not to Word's pages** — treat them as logical
sections (the result carries a `DOCX_RENDITION` warning). Headers, footers, text boxes and floating shapes are not
carried over, and there is no Word / LibreOffice on this machine to render the true layout.

## API

```powershell
# Windows PowerShell 5.1 has no Invoke-RestMethod -Form; use curl.exe
$K = "<your api key>"
curl.exe -s -H "X-API-Key: $K" -F "file=@C:\path\charge_sheet.pdf;type=application/pdf" http://127.0.0.1:8000/api/v1/documents/upload
curl.exe -s -H "X-API-Key: $K" http://127.0.0.1:8000/api/v1/documents/<id>
curl.exe -s -H "X-API-Key: $K" http://127.0.0.1:8000/api/v1/documents/<id>/results
```

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/health` | — | Health, OCR engine, auth configuration |
| POST | `/api/v1/documents/upload` | operator | Upload a PDF, PNG, JPEG, TIFF or DOCX (202, processed asynchronously) |
| GET | `/api/v1/documents` | viewer | List documents |
| GET | `/api/v1/documents/{id}` | viewer | Metadata / processing status |
| GET | `/api/v1/documents/{id}/results` | viewer | Field list (labels and values as printed). `422` with diagnostics (stages, pages, warnings) when extraction failed; `results: null` while processing |
| GET | `/api/v1/documents/{id}/raw` | viewer | Raw JSON: every text line read from the PDF, page by page, as read (text, text layer or OCR, OCR confidence). `?download=true` returns it as `<name>_raw.json`; `404` for documents processed before raw capture (reprocess) |
| GET | `/api/v1/documents/{id}/json` | viewer | **The final JSON shown in the JSON tab**: one key per field (label as printed in the document, repeats get ` (2)`, text before any label is `(text before any label)`), value = complete final text (saved correction, else extracted value), `""` when empty. `?download=true` saves `<name>.json` |
| GET | `/api/v1/documents/{id}/structured` | viewer | **The one final structured JSON**: fixed sections / field names, final value (human correction else machine value) + machine value, status, source page / text, review flags, validation. `?download=true` saves it as `<name>_structured.json`; `404` when the VLM is not enabled or the extraction failed |
| GET | `/api/v1/documents/{id}/source` | viewer | The original upload, byte for byte |
| GET | `/api/v1/documents/{id}/pages/{n}/image?width=` | viewer | PNG of page *n* (from the derived PDF for an image / DOCX) — the side-by-side preview |
| GET | `/api/v1/documents/{id}/corrections` | viewer | Correction / verification audit trail |
| PATCH | `/api/v1/documents/{id}/fields` | operator | Corrections by `field_id`: `p1-03` (as-printed) or `s.case.fir_number` / `s.accused.0.name` (structured); unknown ids → 400 |
| POST | `/api/v1/documents/{id}/verify` | reviewer | Mark verified (notes required) |
| POST | `/api/v1/documents/{id}/reprocess` | operator | Re-run extraction (resets verification) |
| DELETE | `/api/v1/documents/{id}` | admin | Delete PDF, results and corrections |
| GET | `/api/v1/templates` | viewer | Structural fingerprints seen |

## Tests and evaluation

```powershell
pytest -q                               # unit + API + ground-truth regression tests
$env:RUN_OCR_TESTS="1"; pytest -q        # also scanned documents (local OCR, slow)
python scripts\evaluate.py --ocr         # writes docs/ACCURACY_REPORT.md
python scripts\evaluate_variants.py --n 150   # same template, different data (known templates)
python scripts\verify_offline.py
```

VLM pipeline (the default `pytest` run never contacts the model server — a fake stands in for it):

```powershell
pytest tests\test_structured_mapping.py tests\test_vlm_client.py tests\test_vlm_pipeline.py tests\test_structured_eval.py -q
python scripts\verify_ui.py --shots shots     # drives headless Edge/Chrome: Fields + JSON tabs, corrections (no model server)
$env:RUN_VLM_TESTS="1"; pytest tests\test_vlm_live.py -q -s   # real inference (skips if the server is busy)
python scripts\evaluate_structured.py [--private] [--max-pages N] [--wait SECONDS]   # field-level accuracy
```

`evaluate_structured.py` scores the machine values (never human corrections) with strict exact match and
reports wrong, missing and fabricated values separately; it refuses to start while the shared model server is busy.

`evaluate_variants.py` turns each known template into many documents of the same layout with different
names, places, ages, phone numbers, FIR numbers, dates and sections, plus added/removed witnesses and
accused, and checks each against its own ground truth. Template specs live in
`tests/fixtures/private/variant_specs/` (gitignored); add one per new template you need to guarantee.

Ground truth lives in `tests/fixtures/ground_truth/` (synthetic, committed) and `tests/fixtures/private/`
(real documents — **gitignored**, contains personal data; source PDFs are located by SHA-256 under
`uploads/` or `$EVAL_DOCS_DIR`). Each file may list `known_gaps`; the regression test fails on any other
mismatch. Accuracy figures in `docs/ACCURACY_REPORT.md` are counts on a small dataset that was also used
during development — they are not a general accuracy claim. Measure on your own verified documents before
operational use.

## Configuration

See `.env.example`. Important keys: `ENABLE_AUTH`, `API_KEY`, `API_KEYS`, `DATABASE_URL`, `UPLOAD_DIR`,
`RETENTION_DAYS`, `OFFLINE_MODE`, `NETWORK_ALLOWLIST`, `OCR_ENABLED`, `OCR_ENABLE_MKLDNN`,
`PPSTRUCTURE_ENABLED`, `DOCLING_ENABLED`, `VL_ENABLED`, `VL_REQUIRE_GPU`, `STORE_PAGE_TEXT`,
`PROCESSING_TIMEOUT_SECONDS`, `MAX_UPLOAD_SIZE_MB`, `MAX_PAGES`, `CORS_ORIGINS`, and the `VLM_*` keys above.

## Project layout

```text
app/            FastAPI app, services (pipeline), schemas, UI
evaluation/     Ground-truth loading and metrics (never imported by app/)
tests/          Automated tests + fixtures (synthetic PDFs, ground truth)
scripts/        Sample generator, evaluation, offline verification
docs/           Security, audit report, accuracy report
uploads/ data/  Local PDFs and SQLite database (gitignored)
```
