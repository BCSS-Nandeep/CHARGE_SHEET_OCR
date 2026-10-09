# Charge Sheet PDF Extraction Service

Standalone **local-only** FastAPI service with an HTML UI for uploading charge-sheet PDFs, extracting text (PyMuPDF + optional local PaddleOCR), mapping fields to a versioned schema without hardcoded per-template parsers, validating with Pydantic, and exposing results over a REST API.

> **Security:** Designed for confidential police data. External AI APIs are disabled by default. See [docs/SECURITY.md](docs/SECURITY.md).

## Features

- Upload digital or scanned multi-page PDFs (UI + API)
- Template-independent structure detection and synonym-based field mapping
- Traceable fields: original label/value, normalized value, page, confidence, validation/review status
- Corrections stored without overwriting originals
- Async processing with status polling
- SQLite (default) or PostgreSQL
- API key auth, CORS allowlist, upload limits, retention purge, outbound network guard

## Quick start

```powershell
cd C:\Users\Dell\Desktop\CHARGE_SHEET_OCR
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# Edit .env — set a strong API_KEY

python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open:

- UI: http://127.0.0.1:8000/
- Swagger: http://127.0.0.1:8000/docs

Auth is **off by default** for local use (`ENABLE_AUTH=false`). Set `ENABLE_AUTH=true` and an `API_KEY` when exposing the service on a shared network.

### Local OCR for scanned PDFs (required for image-only uploads)

Scanned PDFs produce empty results until a **local** OCR engine is installed. The service does not call cloud OCR.

**Option A — PaddleOCR (preferred):**
```powershell
pip install paddlepaddle paddleocr
```

**Option B — Tesseract (fallback):**
Install [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) for Windows, then:
```powershell
pip install pytesseract
```

Keep `OCR_ENABLED=true`. Prefetch models on a controlled network, then run offline. Check readiness via `GET /health` → `ocr_available`.

## Sample PDFs

No production charge sheets are bundled. Generate four synthetic layout variants:

```powershell
python scripts\generate_sample_pdfs.py
```

Files land in `tests/fixtures/`.

## API examples

```powershell
$H = @{ "X-API-Key" = "dev-local-api-key-change-me" }

# Health (no auth)
Invoke-RestMethod http://127.0.0.1:8000/health

# Upload
$r = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/documents/upload `
  -Headers $H -Form @{ file = Get-Item .\tests\fixtures\sample_01_digital_standard.pdf }
$r.document_id

# Poll status
Invoke-RestMethod http://127.0.0.1:8000/api/v1/documents/$($r.document_id) -Headers $H

# Results
Invoke-RestMethod http://127.0.0.1:8000/api/v1/documents/$($r.document_id)/results -Headers $H
```

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Health / readiness |
| POST | `/api/v1/documents/upload` | Upload PDF |
| GET | `/api/v1/documents` | List documents |
| GET | `/api/v1/documents/{id}` | Metadata / status |
| GET | `/api/v1/documents/{id}/results` | Structured extraction |
| GET | `/api/v1/documents/{id}/source` | Original PDF |
| POST | `/api/v1/documents/{id}/reprocess` | Retry |
| PATCH | `/api/v1/documents/{id}/fields` | Corrections |
| POST | `/api/v1/documents/{id}/verify` | Mark verified |
| GET | `/api/v1/templates` | Template fingerprints |

## Tests

```powershell
pytest -q
python scripts\verify_offline.py
```

## Configuration

See `.env.example`. Important keys: `API_KEY`, `DATABASE_URL`, `UPLOAD_DIR`, `OFFLINE_MODE`, `ALLOW_EXTERNAL_NETWORK`, `OCR_ENABLED`, `RETENTION_DAYS`, `CORS_ORIGINS`.

## Project layout

```text
app/           FastAPI app, services, schemas, UI
tests/         Automated tests + fixtures
scripts/       Sample PDF generator, offline verification
docs/          Security documentation
uploads/       Stored PDFs (local)
data/          SQLite DB (local)
```

## Accuracy notes

Extraction quality depends on PDF text layer / OCR quality. The pipeline:

- Maps known synonyms (FIR / Crime No / PS / Distt / Challan, etc.)
- Extracts numbered accused/witness blocks heuristically
- Flags missing/uncertain values instead of inventing data
- Leaves `requires_human_review=true` until an operator verifies

Measure field-mapping accuracy against manually verified ground truth for your jurisdiction’s forms before operational use.
