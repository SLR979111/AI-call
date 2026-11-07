param(
  [string]$Python = "H:\pytorch-env\python.exe",
  [int]$Port = 8081
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$web = Join-Path $root ".web311"
if (-not (Test-Path $web)) {
  throw "Missing .web311 runtime. Install FastAPI/uvicorn/sherpa-onnx/onnxruntime first."
}
if (-not (Test-Path $Python)) {
  throw "Python 3.11/3.12 not found: $Python"
}
foreach ($p in @("models\asr\tokens.txt", "models\asr\encoder.int8.onnx", "models\melo\model.onnx")) {
  if (-not (Test-Path (Join-Path $root $p))) {
    throw "Missing local voice model: $p"
  }
}

$env:PYTHONPATH = "$web;$env:PYTHONPATH"
$env:ORT_DLL_DIR = Join-Path $web "onnxruntime\capi"
$env:APP_ENV = "dev"
$env:DEV_NO_AUTH = "1"
$env:SCENARIO_ID = "pop_qualification_audit"
$env:ASR_PROVIDER = "sherpa"
$env:TTS_PROVIDER = "melo"
$env:ASR_MODEL_DIR = Join-Path $root "models\asr"
$env:MELO_MODEL_DIR = Join-Path $root "models\melo"
$env:MELO_SPEED = "1.0"
$env:TTS_FALLBACK_PROVIDER = ""
$env:POP_CONTEST = "1"
$env:POP_CONTEST_POLICY = "guided"
# ASCII JSON escapes keep this script readable under Windows PowerShell 5.1.
$env:POP_CONTEST_CONTEXT = '{"request_id":"Q983001","brand":"\u6f14\u793a\u54c1\u724c","reject_l1":"\u5165\u9a7b","reject_l2":"\u516c\u53f8\u4e3b\u4f53","reject_l3":"\u4e3b\u4f53\u8d44\u8d28\u5230\u671f"}'
$env:DATA_DIR = Join-Path $root "data\local"

Write-Host "Starting local sherpa + Melo on http://127.0.0.1:$Port/rtc/"
Write-Host "Model: $($env:ASR_MODEL_DIR) + $($env:MELO_MODEL_DIR)"
Write-Host "Provider status: http://127.0.0.1:$Port/voice/status"
& $Python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
