# Unified police-document OCR (branch `MERGE_MASTER`)

One application that takes a **charge sheet** or an **interrogation report** (PDF, DOCX, PNG, JPEG, TIFF), works out which
it is, sends it to the matching extractor, extracts person images in parallel, and keeps one combined `report_result.json`
per document. **Neither OCR implementation was changed**: IR_OCR is the host, CHARGE_SHEET_OCR is an unmodified snapshot
that runs as its own process, and everything new lives in `unified/` (plus tests, docs and scripts).

```
 browser / API client
        │
 ┌──────▼────────────────────────── unified.app:app ──────────────────────────────────────────────┐
 │  Gateway (ASGI)   routes every request to exactly one of:                                        │
 │   ├─ unified layer ── upload · intake · history · combined report · person images ·              │
 │   │     (unified/)     authenticated forwarding of charge-sheet documents · charge-sheet pages   │
 │   │        Coordinator ─ Classifier ─ Registry ─ Adapters ─ ImageService ─ ReportWriter          │
 │   └─ host ─────────── IR_OCR  app.main:app  (unchanged: its routes, pages, DB, review UI)         │
 └───────┬───────────────────────────────┬──────────────────────────────────────────────────────────┘
         │ in-process (own upload route)  │ HTTP, loopback only
 ┌───────▼──────────────┐        ┌────────▼───────────────────────────────────────────┐
 │ InterrogationReport  │        │ ChargeSheetAdapter ─► worker process                │
 │ Adapter → IR_OCR     │        │   vendor/charge_sheet_ocr  (byte-identical to       │
 │ (app/, unchanged)    │        │   CHARGE_SHEET_OCR@e6d6dd6; own interpreter, DB,    │
 └──────────────────────┘        │   uploads; + one read-only /internal/probe route)   │
                                 └─────────────────────────────────────────────────────┘
```

## Why the charge-sheet extractor is a separate process

Both projects are Python packages named `app`, and the modules of the same name **differ** (`field_builder`,
`field_synonyms`, `form_parser`, `text_normalize`, `pdf_service`, `document_detector`, …). They cannot be imported into one
interpreter, and renaming imports would have meant editing OCR source, which this integration must not do. As a process
the snapshot runs exactly as shipped, with its own database and upload folder; the gateway talks to it over its own HTTP API
on `127.0.0.1`. (Dependencies were not the obstacle: the two `requirements.txt` differ only in `python-docx` 1.2.0 vs 1.1.2.)

The worker is started through `unified/worker_entry/cs_worker_entry.py`, which imports the snapshot's own `app.main:app`
and adds one read-only route, `POST /internal/probe` (text of the first pages, OCR of the top of the first page when it has
no text layer, and the snapshot's own `DocumentDetector` verdict). The same pattern IR_OCR's person-image integration uses.

## Lifecycle of an upload

1. **File check** – the type is decided from the *content* (PDF, DOCX, PNG, JPEG, TIFF); anything else, an empty file, a
   legacy `.doc`, an oversized file is refused before anything is stored.
2. **Original kept once** (`data/unified/originals`, SHA-256 recorded). The OCR service receives this exact file, and so
   does the person-image pipeline.
3. **Document type** – manual (`document_type` field / the page's selector) or automatic. Automatic detection
   needs a *defining phrase* of the form (title-region phrases such as "Interrogation Report", CCTNS, "Charge Sheet",
   "Final Report", Section 173 Cr.P.C. / 193 BNSS, challan) **and** a score ≥ 0.55 **and** a clear lead (≥ 0.25) when both
   forms have such phrases. Signals: content-based file type, text of the first pages (text layer, else OCR of the top of
   page 1), IR_OCR's `DocumentDetector` (in-process), CHARGE_SHEET_OCR's `DocumentDetector` (in the worker), and the
   structure markers added here. Detector hints alone ("accused", "complainant", "brief facts") can never route a document.
   Anything unclear – no readable text, both forms plausible, a FIR, an unrelated memo – becomes an **intake**
   (`awaiting_type`): it is kept, **not sent to any extractor**, and the user chooses a type (or discards it).
4. **Dispatch** – the owning adapter receives the original exactly as a direct upload would. IR_OCR accepts PDF and DOCX only,
   so an *image* can only go to the charge-sheet service; choosing IR for an image is refused (HTTP 415) with a clear message.
5. **Registry row** (document id = the owning service's id, so every existing API path keeps working), then:
   a watcher that rebuilds `report_result.json` when OCR is final, and - **for interrogation reports only** - person-image
   extraction on its own thread, in parallel with OCR. **Charge sheets get no person-image extraction**: no run is ever
   started, the Person Images tab is not shown, and the export reports `image_extraction_status: "not_applicable"`.

## Request routing (gateway)

| Request | Goes to |
|---|---|
| `GET /`, `POST /api/v1/documents/upload`, `GET /api/v1/documents`, `/api/v1/unified/*`, `/unified-static/*`, `/cs-static/*` | unified layer |
| `…/documents/{id}/report_result`, `…/person-images…` (any document) | unified layer |
| any other `/api/v1/documents/{id}…`, `/results/{id}` of a **charge sheet** | unified layer → authenticated, then forwarded to the worker |
| the same for an **interrogation report** or an id the registry does not know (uploaded before the merge) | IR_OCR host, untouched |
| `/health`, `/history`, `/docs`, `/static/*`, … | IR_OCR host, untouched |

The host's `/results/{id}` page (and the charge-sheet page, whose static files are served under `/cs-static/` so its
`app.js` cannot clash with IR's) get one extra script and stylesheet: a banner (type, how it was decided, both pipelines'
status, warnings, "Download combined JSON") and the **Person Images** tab. Fields / JSON / correction / verification /
history UIs are each service's own and behave as before.

After a response: `reprocess` → new image run and a fresh export; saved corrections / verification → fresh export;
`DELETE` → registry tombstone and removal of the layer's own copy of the original.

## API added

| | |
|---|---|
| `POST /api/v1/documents/upload` | `file` + optional `document_type` (`auto` \| `charge_sheet` \| `interrogation_report`). `202`; body has `document_id`, `status`, `document_type`, `document_type_source`, `classification`. If the type is unclear: `status: "awaiting_type"`, `document_id: null`, `intake_id`. |
| `POST /api/v1/unified/classify` | dry run: what would this file be routed as? Nothing stored. |
| `GET /api/v1/unified/intakes`, `POST …/intakes/{id}/route {"document_type"}`, `DELETE …/intakes/{id}` | uploads waiting for a type |
| `GET /api/v1/unified/documents/{id}` | coordinator view: both pipelines, review, warnings/errors, provenance |
| `GET /api/v1/documents/{id}/report_result[?download=true]` | the combined JSON, rebuilt from persisted state |
| `GET/POST /api/v1/documents/{id}/person-images…` | same API as the IR person-image integration, for interrogation reports; for a charge sheet `GET` answers `not_applicable` and `POST …/run` is refused (409) |
| `GET /api/v1/documents` | history of both services (each item has `document_type`) |
| `GET /api/v1/unified/health` | layer + worker status |

Authentication and roles are the host's (`API_KEY`/`API_KEYS`, viewer < operator < reviewer < admin; with
`ENABLE_AUTH=false` only loopback clients are served and nobody can verify). Calls for charge sheets are authenticated here
first, then forwarded with the caller's own key, which the managed worker accepts because it is started with the same key
table (so reviewer names are attributed correctly). The worker listens on loopback only.

## `report_result.json` – envelope version **2.0.0**

Schema: `unified/report_result.schema.json`. One file per document at `output/<file name>_<ext>/report_result.json`.

| Block | Content |
|---|---|
| `schema_version` | **envelope** version, `2.0.0`. Independent of every OCR schema version. |
| `document` | original file name, `document_type`, `document_type_source` (`auto`/`manual`/`legacy`), detected kind, size, SHA-256, intake id |
| `classification` | how the type was decided: file facts, text source, both detectors' verdicts, markers, scores, reasons, warnings (no document text) |
| `processing` | `ocr_status`, `ocr_document_status`, `ocr_extraction_status`, `ocr_error_summary`, `ocr_progress`, `image_extraction_status`, `image_extraction_error_summary`, `warnings`, `errors` |
| `ocr` | `pipeline` (`ir_ocr`/`charge_sheet_ocr`), `schema_version` (**the service's own**, copied) and `payload` = exactly what the service's `GET …/results` returned, or `null` |
| `person_images` | as in 1.0.0: latest status, newest usable run, images (relative paths, never bytes) |
| `review` | `status`, `verified`, `verified_by`, `requires_review`, `corrected_fields` (the corrections themselves are in `ocr.payload.field_corrections`; the full history stays in the service) |
| `provenance` | unified + envelope version, native OCR schema version, extractor repository / branch / commit / mode |

Versioning: 1.0.0 was the IR-only envelope of the person-image integration; `processing`, `ocr` and `person_images` keep
their 1.x meaning, so 2.0.0 adds blocks rather than reinterpreting them – but it is a new major version because the
`document_type` axis and the `pipeline` value change what `ocr.payload` can be. The native schema versions are untouched:
IR_OCR `results.schema_version` stays `2.0.0`, the charge-sheet service reports its own.

The file is derived: delete it and it is rebuilt. It is rebuilt when OCR reaches a final state, when an image run changes
state, after saved corrections and verification, and after reprocessing. A pipeline that failed is reported as failed while
the other pipeline's data is kept (OCR failure keeps images; image failure keeps `ocr.payload`).

## Where things are stored

| What | Where |
|---|---|
| IR_OCR documents, results, corrections | IR_OCR's own database/uploads (`DATABASE_URL`, `UPLOAD_DIR`) – a **new** empty one in this folder |
| Charge-sheet documents, results, corrections | the worker's own database/uploads under `data/unified/charge_sheet_worker/` |
| Registry (ownership, routing decision, event log), kept originals | `data/unified/registry.db`, `data/unified/originals/` |
| Person images, `manifest.json`, `report_result.json` | `output/` (`IR_PERSON_IMAGES_OUTPUT`) |

**No existing data was migrated, merged or deleted.** The databases in `IR_OCR\data` and `CHARGE_SHEET_OCR\data` are
untouched and not read. A document that was uploaded to IR_OCR before the merge and lives in this folder's IR database is
treated as an interrogation report (`document_type_source: "legacy"`). Merging the old databases into the new ones, if ever
wanted, needs its own plan and approval.

## Running

```powershell
.\run_unified.ps1            # http://127.0.0.1:8000  (python -m uvicorn unified.app:app)
```
Settings: `unified/.env.unified.example`. The charge-sheet worker reads `vendor/charge_sheet_ocr/.env` (a git-ignored copy
of the original project's settings: OCR, vision model, offline mode) and needs an interpreter with PaddleOCR for scanned
charge sheets (`CS_WORKER_PYTHON`). The launcher uses `vendor\charge_sheet_ocr\.venv`, else `..\CHARGE_SHEET_OCR\.venv`.

## Tests and integrity

```powershell
python -m pytest -q                                   # IR_OCR's own tests + person-image tests + tests/unified_layer
python -m pytest tests/unified_layer -m "not real_worker" -q   # without starting the real charge-sheet process
cd vendor\charge_sheet_ocr; <worker python> -m pytest -q       # CHARGE_SHEET_OCR's own tests, in its own folder
python scripts/verify_integrity.py --charge-sheet-repo C:\path\to\CHARGE_SHEET_OCR
```
`verify_integrity.py` shows that every IR_OCR file equals the base commit (additions only) and that every vendored file
equals the blob of CHARGE_SHEET_OCR@e6d6dd6 (`unified/vendor_manifest.json`). `vendor/charge_sheet_ocr/.gitattributes`
(`* -text`) keeps the snapshot byte-identical on every checkout.

## Known limitations and open decisions

* **Scanned / image uploads are classified by OCR of the top of page 1** (about 30–60 s on this CPU; PaddleOCR in the
  worker). Choosing the type yourself skips it. If the worker is not running, such files wait for a manual choice.
* **IR_OCR does not accept images** (PNG/JPEG/TIFF); an image of an interrogation report cannot be extracted by either
  service and is refused when IR is chosen. Converting it to PDF inside this layer was not done (it would be new
  pre-processing in front of an OCR implementation).
* **Person-image extraction is for interrogation reports only** (PDF and DOCX, as before). Charge sheets are not put through it.
* **Retention:** IR_OCR and the charge-sheet service purge old documents by `RETENTION_DAYS`; the layer's kept original, the
  person images and `report_result.json` are not purged by that policy (as with the earlier person-image integration).
  Deleting a document removes the layer's copy of the original, not the images/export. Decide whether to extend retention.
* **Shared model server:** both services use the same local vision model (llama.cpp handles one request at a time). The
  layer adds no model calls, but concurrent uploads of both types queue behind each other at that server.
* **UI details:** the history page (IR_OCR's, unchanged) has no document-type column – the type is in the API, the banner
  and the export; the status line of the charge-sheet page shows the host's `/health` values (so its "vision model" wording
  can differ from the worker's real configuration).
* **Not verified here:** accuracy on real documents is *not* re-measured by these tests (they prove the unified path returns
  what each service returns on its own). See the final report for what was run on real documents.
