# Dev-only split frontend. Serves web/rtc as static files on 5173.
# Backend must be started separately with:
#   $env:DEV_CORS_ORIGINS = "http://127.0.0.1:5173"
param(
  [int]$Port = 5173,
  [string]$Backend = "http://127.0.0.1:8087"
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$webRoot = Join-Path $root 'web\rtc'
if (-not (Test-Path (Join-Path $webRoot 'index.html'))) {
  throw "web/rtc/index.html not found"
}
$python = 'H:\pytorch-env\python.exe'
if (-not (Test-Path $python)) { $python = (Get-Command python).Source }
Set-Location -LiteralPath $webRoot
Write-Host "Dev frontend: http://127.0.0.1:$Port/?app=$Backend"
Write-Host "Start backend with DEV_CORS_ORIGINS=http://127.0.0.1:$Port"
& $python -m http.server $Port --bind 127.0.0.1
