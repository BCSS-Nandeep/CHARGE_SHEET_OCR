# Interrogation Report (IR) OCR / Extraction Service

Standalone **local-only** FastAPI service with an HTML UI for uploading interrogation
report **DOCX** or **PDF** files, detecting the **labels and values printed in each
document** (no fixed field list), and exposing the result over a REST API and an
editable review UI.

Built from the same pipeline as [CHARGE_SHEET_OCR](../CHARGE_SHEET_OCR): schema
**2.0.0** JSON (`fields[]` with `id`, `label`, `value`, `page`, `source`, …).

> **Security:** designed for confidential police data. No document content leaves the host.

## What it extracts

IR forms (CCTNS / MO Criminal Database Part-II) typically include:

1. Individual particulars (name, alias, father, age, address, …)
2. Unique identifiers (mobile, Aadhaar, PAN, …)
3. Physical features
4. Socio / economic profile
5. Family history & associates
6. Period of offence, MO, arrest, previous cases, drugs, finance, SIM, legal support, …

The **document decides the fields**: each result is one list of fields in reading
order with the label and value exactly as printed.

## Quick start (Windows PowerShell)

```powershell
cd C:\Users\Dell\Desktop\IR_OCR
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then edit .env if needed
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Or: `.\run.ps1`

- UI: http://127.0.0.1:8000/
- API docs: http://127.0.0.1:8000/docs

## Upload example

```powershell
curl.exe -s -F "file=@C:\path\Interrogation_report.docx;type=application/vnd.openxmlformats-officedocument.wordprocessingml.document" http://127.0.0.1:8000/api/v1/documents/upload
curl.exe -s http://127.0.0.1:8000/api/v1/documents/<id>/results
```

## JSON shape (same as charge-sheet OCR 2.0.0)

```json
{
  "schema_version": "2.0.0",
  "document_info": {
    "document_type": "interrogation_report",
    "classification_confidence": 0.9
  },
  "fields": [
    {
      "id": "p1-01",
      "page": 1,
      "pages": [1],
      "serial": "1",
      "label": "Name",
      "value": "Mallidi Sai Kiran reddy",
      "source": "form_row",
      "text_block": false,
      "confidence": null,
      "review_status": "unreviewed"
    }
  ],
  "extraction_status": "complete",
  "requires_human_review": true
}
```

## DOCX vs PDF

| Input | Path |
|---|---|
| `.docx` IR Word forms | Native table + heading parse (no OCR) |
| Digital / scanned `.pdf` | Same hybrid stack as charge-sheet OCR (PyMuPDF → PaddleOCR → …) |

## Configuration

See `.env.example`. Important keys: `ENABLE_AUTH`, `DATABASE_URL`, `UPLOAD_DIR`,
`OCR_ENABLED`, `OFFLINE_MODE`.
