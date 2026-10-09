# Security Configuration — Charge Sheet Extraction Service

This service processes confidential police charge sheets. Security is a mandatory acceptance criterion.

## Architecture (offline-first)

```text
Browser / API client
        │  (local network only; API key required)
        ▼
FastAPI (127.0.0.1 by default)
        │
        ├─► Upload store (local disk under UPLOAD_DIR)
        ├─► SQLite / private PostgreSQL
        ├─► PyMuPDF (in-process text extraction)
        └─► PaddleOCR (optional, local process only)
```

**No cloud AI, telemetry, or third-party document processing endpoints are used by default.**

## Data flow

1. Client uploads a PDF with `X-API-Key`.
2. Server validates magic bytes, size, and type; stores under a UUID filename.
3. Processing runs in-process (thread pool). Text is extracted with PyMuPDF.
4. Pages with insufficient text may be OCR’d locally with PaddleOCR if installed.
5. Field mapping and Pydantic validation run in Python on this host.
6. Structured results and corrections are stored in the configured database.
7. Retention job soft-deletes expired documents and removes files.

## External network dependencies

| Dependency | Required | Notes |
|---|---|---|
| PyPI packages (install time only) | Yes, at setup | Install on a controlled network; runtime can be offline |
| PaddleOCR model download | Optional, first OCR use | Prefetch models on a secure host; do not expose charge-sheet images |
| Cloud LLM / OpenAI / etc. | **No** | `LLM_ENABLED=false`, `LLM_ALLOW_EXTERNAL=false` |
| Telemetry | **No** | `DISABLE_TELEMETRY=true` |

Outbound connections are best-effort blocked when `OFFLINE_MODE=true` and `ALLOW_EXTERNAL_NETWORK=false` via `app/security/network_guard.py`. This does **not** replace OS firewall rules.

## Access control

- `ENABLE_AUTH=true` (default): all `/api/v1/*` routes require `X-API-Key`.
- Change `API_KEY` from the example value before any real deployment.
- CORS is restricted to `CORS_ORIGINS`.
- Source PDFs are served only by document ID after authentication.
- Filenames are server-generated; path traversal is rejected.

## Logging and PII

- `LOG_PII=false` (default): phone numbers, Aadhaar-like IDs, and emails are redacted.
- Document body text and narratives must not be written to logs (audit events use IDs/metadata only).
- Exception handlers return generic messages to clients.

## Storage and retention

- Uploads: `UPLOAD_DIR` (default `./uploads`) — keep on encrypted volumes in production.
- Database: `DATABASE_URL` — SQLite for local dev; use private PostgreSQL for multi-user deployments.
- `RETENTION_DAYS` controls automatic purge (default 90). Set according to policy.
- Corrections never overwrite `original_value`.

## Verification tests

```bash
pytest tests/test_security.py tests/test_api.py -q
python scripts/verify_offline.py
```

`verify_offline.py` checks settings, confirms LLM external fallback is disabled, and attempts a blocked outbound connect under the network guard.

## Claims

Do **not** claim the deployment is fully offline or secure until:

1. Dependencies are installed and model caches are local.
2. `OFFLINE_MODE=true` / `ALLOW_EXTERNAL_NETWORK=false` are set.
3. Network guard / firewall tests pass.
4. Auth is enabled with a strong API key.
5. Storage encryption and OS access controls are in place for your environment.
