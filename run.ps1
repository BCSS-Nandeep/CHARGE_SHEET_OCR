# Start the Interrogation Report Extraction Service (local bind)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .\.venv\Scripts\Activate.ps1)) {
  & "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv
  .\.venv\Scripts\Activate.ps1
  pip install -r requirements.txt
} else {
  .\.venv\Scripts\Activate.ps1
}
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
