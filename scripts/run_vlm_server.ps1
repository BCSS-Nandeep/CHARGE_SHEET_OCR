# Start the local Nanonets-OCR2-3B vision-language server (llama.cpp, CPU) on 127.0.0.1.
#
#   .\scripts\run_vlm_server.ps1                 # start in the foreground (Ctrl+C stops it)
#   .\scripts\run_vlm_server.ps1 -Threads 12     # tune CPU threads
#
# The app talks to it through LLM_BASE_URL (see .env.example). Binding to loopback keeps
# report content on this machine. Install layout (outside the repo, several GB):
#   <ModelRoot>\llama.cpp-b11580\llama-server.exe
#   <ModelRoot>\Nanonets-OCR2-3B-GGUF\Nanonets-OCR2-3B.Q8_0.gguf
#   <ModelRoot>\Nanonets-OCR2-3B-GGUF\Nanonets-OCR2-3B.mmproj-f16.gguf
param(
  [string]$ModelRoot = "C:\Users\Dell\IR_OCR_models",
  [int]$Port = 8081,
  [int]$Threads = 8,        # decoding is memory-bound: more threads than P-cores does not help
  [int]$BatchThreads = 8,   # measured on an i7-14700: 20 batch threads was no faster than 8
  [int]$Context = 12288,    # a 150-dpi A4 page is ~2.8k image tokens, plus up to 4k output tokens
  [string]$Alias = "nanonets-ocr2-3b"
)
$ErrorActionPreference = "Stop"
$server = Join-Path $ModelRoot "llama.cpp-b11580\llama-server.exe"
$model  = Join-Path $ModelRoot "Nanonets-OCR2-3B-GGUF\Nanonets-OCR2-3B.Q8_0.gguf"
$mmproj = Join-Path $ModelRoot "Nanonets-OCR2-3B-GGUF\Nanonets-OCR2-3B.mmproj-f16.gguf"
foreach ($p in @($server, $model, $mmproj)) {
  if (-not (Test-Path $p)) { throw "Missing $p - see the install notes at the top of this script." }
}
& $server -m $model --mmproj $mmproj --alias $Alias `
  --host 127.0.0.1 --port $Port -c $Context -t $Threads -tb $BatchThreads -np 1 --no-webui
