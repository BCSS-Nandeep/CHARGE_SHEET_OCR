# Person images for IR_OCR

One upload through the existing IR_OCR screen now gives the OCR result **and** the person images found in the document,
shown in a **Person Images** tab, plus one combined `report_result.json` per document.

**No existing OCR file was changed.** The integration is a separate package that attaches to the existing app from the
outside (`app.main:app` is imported as it is). Starting `app.main:app` directly still works exactly as before, without
the image features.

## Run

```powershell
pip install -r integrations/person_images/requirements.txt      # opencv-python-headless (only extra dependency)
.\run_integrated.ps1                                              # or:
python -m uvicorn integrations.person_images.app:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000, upload a PDF/DOCX, open the result: **Fields / JSON / Person Images**.

## What happens on an upload

```
browser ──POST /api/v1/documents/upload──▶ existing OCR route (unchanged) ──202──▶ browser
                                              │
        integration middleware sees the 202 (document_id in the body), after the response is sent:
          ├─ service.start(id)  ─▶ own thread: read the stored upload (read-only) ─▶ copied extractor ─▶
          │                          output/<id>/person_images/runs/<run>/person_001.jpeg … + manifest.json
          └─ watch OCR (DB, read-only) ─▶ when OCR is final ─▶ output/<id>/report_result.json
```

The two pipelines share nothing but the document id: an image-extraction failure is recorded as a failed run and never
touches OCR results; an OCR failure never removes the images. The middleware also reacts to reprocess (new image run),
`PATCH …/fields` and `POST …/verify` (refresh `report_result.json`). The database stays the source of truth for OCR,
corrections and verification; `report_result.json` is derived and can be deleted or rebuilt at any time.

## Files

| New file | Purpose |
|---|---|
| `extractor.py` | copy of `REPORT_IMAGE_EXTRACTOR/extract_images.py` (see its header: what is verbatim, what is adapted) |
| `models/face_detection_yunet_2023mar.onnx` | the YuNet model, byte-identical copy |
| `store.py` | output folders, manifest, strict id / filename validation, atomic writes |
| `service.py` | runs an extraction in its own thread, records the outcome |
| `report_result.py`, `report_result.schema.json` | the combined export and its schema |
| `routes.py`, `hooks.py`, `wiring.py`, `app.py` | API routes, response hooks, attach(), ASGI entry |
| `static/person_images.js/.css` | the Person Images tab |

## API (new routes; same API-key rules as the rest)

| | |
|---|---|
| `GET  /api/v1/documents/{id}/person-images` | status, images of the newest usable run, run history |
| `GET  /api/v1/documents/{id}/person-images/files/{run}/{name}` | one image (needs the key header, like every route) |
| `POST /api/v1/documents/{id}/person-images/run` | run the extraction again (operator) |
| `GET  /api/v1/documents/{id}/report_result[?download=true]` | rebuild + return the combined JSON |

## Storage (`output/` next to the app; `output/.gitignore` keeps it out of git)

```
output/<uploaded file name>_<ext>/person_images/<file name> image.jpeg
output/<uploaded file name>_<ext>/person_images/<file name> image (2).jpeg
output/<uploaded file name>_<ext>/person_images/<file name> image (3).jpeg
output/<uploaded file name>_<ext>/person_images/manifest.json
output/<uploaded file name>_<ext>/person_images/rerun_2/<file name> image.jpeg    <- a reprocess keeps its own folder
output/<uploaded file name>_<ext>/report_result.json
output/.person_images_index.json                                                   <- document id -> folder
```

* The folder is the uploaded file's name plus `_docx` / `_pdf` (as in the original extractor, so `a.pdf` and `a.docx` stay
  apart). Images are `<file name> image.<ext>`, then `<file name> image (2).<ext>`, `(3)`, ...
* Names are sanitised: path separators, the characters Windows forbids, control characters, leading/trailing dots and spaces
  are replaced or removed; Windows reserved names (`CON`, `NUL` ...) are prefixed; other scripts (for example Telugu) are
  kept; and the length is bounded so the full path stays under the Windows limit (a deep output location shortens the
  names, so keep the `output/` path short).
* Two different documents uploaded with the same file name get `<name>_docx` and `<name>_docx (2)`. The index and each manifest
  record which document owns which folder, so images always belong to the right document.
* The first run saves straight into `person_images/`; each later run (a reprocess, or "Run image extraction again") goes into
  `person_images/rerun_<n>/`. Nothing is deleted or overwritten: a run folder that already holds images is refused, each file
  is opened in exclusive-create mode, and the API serves only files that the manifest lists.
* Folders made by an earlier version (named by document id, images under `runs/<run_id>/person_001.jpeg`) are still found
  and shown.
* Override the location with the `IR_PERSON_IMAGES_OUTPUT` environment variable.

## `report_result.json` (envelope `schema_version` 1.0.0, see `report_result.schema.json`)

```json
{
  "schema_version": "1.0.0",
  "document_id": "…",
  "generated_at": "2026-10-15T04:30:00Z",
  "processing": {
    "ocr_status": "succeeded", "ocr_document_status": "completed", "ocr_extraction_status": "complete",
    "ocr_error_summary": null,
    "image_extraction_status": "succeeded", "image_extraction_error_summary": null
  },
  "ocr": { "schema_version": "<payload.results.schema_version>", "payload": { "document": {…}, "results": {…}, "field_corrections": {…} } },
  "person_images": {
    "status": "succeeded", "run_id": "run_…", "total_images_found": 3, "skipped_images": 1, "unreadable_images": 0,
    "images": [ { "file": "person_images/Report image.png", "name": "Report image.png", "source_page": null,
                  "source_member": "image1.png", "width": 160, "height": 200, "faces": 1, "size_bytes": 61256, "sha256": "…" } ]
  }
}
```

`ocr.payload` is exactly what `GET /api/v1/documents/{id}/results` returns (nothing renamed or recomputed); it is `null`
when OCR produced nothing. **The OCR schema version in this project is `2.0.0`** (`results.schema_version`); the envelope
copies whatever the payload says. Paths are relative to the document's folder (`output/<uploaded file name>_<ext>/`); image
bytes are never embedded.

Image statuses: `queued`, `processing`, `succeeded`, `partial` (some images could not be read), `no_person_images`,
`failed`, `unsupported`. The top-level status is the **latest** run's, while `run_id` / `images` are those of the newest
usable run, so a failed re-run is visible without hiding earlier images.

## Detection rules (unchanged from the original)

Face score ≥ 0.7 (≥ 0.8 for rotated attempts), minimum image side 40 px, downscale to 1600 px, rotations tried in the order
0°, 90°, 270°, 180°; EXIF rotation honoured. PDFs: embedded images (each object once); DOCX / XLSX: `word|xl/media/`.
The OCR upload accepts PDF and DOCX only, so XLSX is supported by the copied code but cannot arrive through this app.

## Tests

```powershell
python -m pytest tests/person_images -q          # new tests (need opencv-python-headless)
python -m pytest -q                              # everything, as a regression check
```

Optional positive control with a real face photo: set `IR_TEST_FACE_IMAGE` to its path.

## Known limitations

* **Start command.** The features are active only when started through `integrations.person_images.app:app`
  (`run_integrated.ps1`). `run.ps1` / `app.main:app` run the plain OCR app.
* **Deleting a document / retention.** The existing `DELETE /documents/{id}` and the retention purge remove the upload and
  OCR results but know nothing about `output/<id>/`; person images (personal data) stay until removed manually.
* **Reprocess = new run.** Previous runs are kept by design, so disk use grows with each reprocess.
* **Detector** is the original's (YuNet): it finds faces, not identities, and can miss unusual photos or accept a face-like
  drawing. Undecodable PDF image objects are skipped silently by the original code (not counted).
* The Person Images tab was checked by tests of the API/HTML and a syntax check of the script; it has not been clicked
  through in a browser by the author.
