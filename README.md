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
- **raw_json** tab in the UI and `/raw` endpoint: every line read from each page exactly as read (text layer
  or OCR), top to bottom, with OCR confidence — nothing dropped, grouped or corrected; stored
  locally next to the results and deleted with the document
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
| POST | `/api/v1/documents/upload` | operator | Upload a PDF (202, processed asynchronously) |
| GET | `/api/v1/documents` | viewer | List documents |
| GET | `/api/v1/documents/{id}` | viewer | Metadata / processing status |
| GET | `/api/v1/documents/{id}/results` | viewer | Field list (labels and values as printed). `422` with diagnostics (stages, pages, warnings) when extraction failed; `results: null` while processing |
| GET | `/api/v1/documents/{id}/raw` | viewer | Raw JSON: every text line read from the PDF, page by page, as read (text, text layer or OCR, OCR confidence). `?download=true` returns it as `<name>_raw.json`; `404` for documents processed before raw capture (reprocess) |
| GET | `/api/v1/documents/{id}/source` | viewer | Original PDF |
| GET | `/api/v1/documents/{id}/corrections` | viewer | Correction / verification audit trail |
| PATCH | `/api/v1/documents/{id}/fields` | operator | Corrections by `field_id` (e.g. `p1-03`; unknown ids → 400) |
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
`PROCESSING_TIMEOUT_SECONDS`, `MAX_UPLOAD_SIZE_MB`, `MAX_PAGES`, `CORS_ORIGINS`.

## Project layout

```text
app/            FastAPI app, services (pipeline), schemas, UI
evaluation/     Ground-truth loading and metrics (never imported by app/)
tests/          Automated tests + fixtures (synthetic PDFs, ground truth)
scripts/        Sample generator, evaluation, offline verification
docs/           Security, audit report, accuracy report
uploads/ data/  Local PDFs and SQLite database (gitignored)
```
