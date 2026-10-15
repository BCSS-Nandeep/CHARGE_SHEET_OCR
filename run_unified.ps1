# Start the unified application: one upload page for charge sheets and interrogation reports.
#   - the IR_OCR application is the host (unchanged), the unified layer sits in front of it;
#   - the charge-sheet extractor (vendor\charge_sheet_ocr, unchanged) is started on demand as its own process.
#   .\run_unified.ps1                      # http://127.0.0.1:8000
#
# Charge-sheet OCR needs an interpreter with PaddleOCR. Order of choice:
#   $env:CS_WORKER_PYTHON, then vendor\charge_sheet_ocr\.venv, then ..\CHARGE_SHEET_OCR\.venv (the original project's
#   environment, if it sits next to this folder), then this application's own interpreter (digital-text PDFs/DOCX only).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .\.venv\Scripts\Activate.ps1)) {
  & "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv
  .\.venv\Scripts\Activate.ps1
  pip install -r requirements.txt
} else {
  .\.venv\Scripts\Activate.ps1
}
# exits 1 (silently) when OpenCV is missing; no stderr text, so $ErrorActionPreference = "Stop" is not triggered
python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('cv2') else 1)"
if ($LASTEXITCODE -ne 0) { pip install -r integrations/person_images/requirements.txt }
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
if (-not (Test-Path .\vendor\charge_sheet_ocr\.env)) { Copy-Item .\vendor\charge_sheet_ocr\.env.example .\vendor\charge_sheet_ocr\.env }

if (-not $env:CS_WORKER_PYTHON) {
  foreach ($candidate in @(".\vendor\charge_sheet_ocr\.venv\Scripts\python.exe", "..\CHARGE_SHEET_OCR\.venv\Scripts\python.exe")) {
    if (Test-Path $candidate) { $env:CS_WORKER_PYTHON = (Resolve-Path $candidate).Path; break }
  }
}
if ($env:CS_WORKER_PYTHON) { Write-Host "Charge-sheet worker interpreter: $env:CS_WORKER_PYTHON" }
else { Write-Warning "No separate charge-sheet interpreter found; scanned charge sheets need PaddleOCR (set CS_WORKER_PYTHON)." }

python -m uvicorn unified.app:app --host 127.0.0.1 --port 8000
