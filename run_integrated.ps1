# Start IR_OCR with the person-image integration (OCR results + Person Images tab + combined report_result.json).
# The OCR application itself is unchanged; this only starts it through the integration's entry point.
#   .\run_integrated.ps1
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
python -m uvicorn integrations.person_images.app:app --host 127.0.0.1 --port 8000
