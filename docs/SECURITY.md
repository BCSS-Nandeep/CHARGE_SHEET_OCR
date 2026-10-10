# Security — Charge Sheet Extraction Service

This service processes confidential police charge sheets. This document states the security design, what
has been verified, and what has **not** — passing the offline-guard test is not proof of zero leakage.

## Architecture (offline-first)

```text
Browser (same host or LAN, API key)
        │
FastAPI (127.0.0.1 by default)
        ├─ Upload store (UPLOAD_DIR, server-generated names)
        ├─ SQLite / private PostgreSQL (results, corrections, audit trail)
        ├─ PyMuPDF — in-process text/table extraction
        └─ PaddleOCR / Tesseract — in-process / local binary OCR, models cached on disk
```

No cloud AI, OCR, telemetry or error-reporting service is called. There is no LLM integration in the code;
the `LLM_*` settings are placeholders that `scripts/verify_offline.py` checks stay local-only.

## Data flow

1. Client uploads a PDF. The server reads it in 1 MB chunks (rejecting > `MAX_UPLOAD_SIZE_MB` early), checks
   the `%PDF` header, and stores it under `<uuid>_<sanitised name>.pdf` inside `UPLOAD_DIR` (traversal-proof).
2. A background worker extracts text (PyMuPDF). Pages without a text layer are rendered to PNG **in memory**
   and OCR'd locally. No page images or text are written to disk or logs.
3. Parsing, mapping, validation and conflict detection run in-process.
4. Results (which include personal data) are stored in the database, and so is the raw text of every page
   (`results.raw_json`, served by `GET /api/v1/documents/{id}/raw` to any valid API key, same as results). It is
   never logged (the audit log records only that it was viewed) and is removed with the document.
5. Corrections are stored separately, with the extracted value they replaced and the reviewer name.
6. Retention (`RETENTION_DAYS`) and `DELETE /api/v1/documents/{id}` remove the PDF, results and corrections;
   a tombstone row without content or file name is kept for the audit trail.

## Outbound network

| Dependency | When | Control |
|---|---|---|
| PyPI packages | install time only | install on a controlled network |
| PaddleOCR models | first use only | prefetch on a controlled network; at runtime `PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK` is forced on and the network guard blocks downloads |
| Swagger UI assets (`/docs`) | browser, when /docs is opened | `API_DOCS_ENABLED=false` on air-gapped networks |
| Cloud LLM / OCR / telemetry | never | not in the code |

`OFFLINE_MODE=true` installs a best-effort guard (`app/security/network_guard.py`) that patches
`socket.connect` and `socket.connect_ex` to refuse non-loopback, non-private addresses (RFC 1918 ranges via
`ipaddress`; hostnames must be listed in `NETWORK_ALLOWLIST`). **Limits:** it does not see native code that
opens sockets outside Python, DNS lookups are not blocked, and it does not replace an OS firewall. For real
assurance, run the host with outbound traffic denied at the firewall.

## Access control

- `ENABLE_AUTH=false` (default, for a single local user): only loopback clients are served; they act as an
  unauthenticated *operator* and **cannot verify** results.
- `ENABLE_AUTH=true`: API keys in the `API_KEY_HEADER` header. Keys map to a user name and role
  (`viewer < operator < reviewer < admin`, see README). Keys shorter than 16 characters and the example
  values are refused; with no usable key the API answers 503 and `/health` reports `auth_misconfigured`.
- Verification requires the reviewer role and notes; the reviewer name is recorded on the document and in the
  audit trail. Corrections record who made them.
- The deployment is single-tenant: every authenticated user can see every document. There is no per-user
  document ownership. If several units share one instance, run separate instances (separate databases and
  upload directories) per unit.
- CORS is limited to `CORS_ORIGINS`, without credentials. UI pages send a strict CSP (`script-src 'self'`, no
  inline script), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`; API responses and result
  pages are `Cache-Control: no-store`.
- The UI keeps the API key in `sessionStorage` (forgotten when the tab closes).

## Logging and PII

- Audit events log document IDs, counts, statuses and user names — never document text, names or file names.
- `LOG_PII=false` additionally redacts phone numbers, Aadhaar-like numbers and e-mail addresses from log
  messages and exception text.
- Exceptions return generic messages to clients; stored error messages are truncated and contain no page text.

## Development data hygiene

- Real-document ground truth and evaluation details live in `tests/fixtures/private/` (gitignored).
- `tests/test_no_sample_hardcoding.py` fails if any value from any ground-truth file (names, places, FIR
  numbers, dates) appears anywhere in `app/`, comments included.
- **Action needed:** commit `9198568` contains real personal data from one charge sheet
  (`tests/fixtures/ground_truth_fir83_2020.json`, `docs/ACCURACY_REPORT_FIR83.md`, names in
  `tests/test_no_sample_hardcoding.py`). These files are removed or replaced in the working tree, but they
  remain in git history. If this repository is or will be shared, rewrite the history (e.g. `git filter-repo`)
  before pushing.
- Older debug scripts wrote full document text to `tmp_debug_*.txt` in the project root; the scripts are
  removed, but any existing `tmp_debug_*.txt` files still contain document text and should be deleted.

## Verification performed (2026-10-09)

| Check | Result |
|---|---|
| `pytest` security/API tests (auth roles, loopback-only mode, placeholder keys, upload limits, path traversal, network guard incl. `connect_ex` and 172.32/12 boundary, retention purge, security headers) | pass |
| `python scripts/verify_offline.py` | see command output |
| Real OCR run with the network guard active (PaddleOCR, cached models) | completed without outbound connections being needed |

## Not verified / out of scope

- Encryption at rest (use an encrypted volume for `UPLOAD_DIR` and the database).
- TLS (put the service behind a TLS-terminating reverse proxy if it is reachable from other machines).
- Per-user / per-tenant document isolation (see Access control).
- Supply-chain review of third-party packages. The OCR stack (paddlex) pulls in model-download clients such as
  `huggingface_hub` (and its `httpx2` dependency); they are not called at runtime once models are cached, and
  the network guard blocks them if they try.
