param([int]$Port = 8088, [switch]$OpenBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    & uv sync --group dev
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.env'))) {
    throw 'Create .env from .env.example and supply your API credential first.'
}
$runtimePath = Join-Path $projectRoot 'runtime'
New-Item -ItemType Directory -Force -Path $runtimePath | Out-Null
$serverUrl = "http://127.0.0.1:$Port"
try {
    $existing = Invoke-RestMethod -Uri "$serverUrl/healthz" -TimeoutSec 2
    if ($existing.name -eq 'ai-call-g2') {
        Write-Output "Already running: $serverUrl"
        if ($OpenBrowser) { Start-Process $serverUrl }
        exit 0
    }
    throw "Port $Port belongs to another application."
} catch {
    if ($_.Exception.Message -like '*belongs to another application*') { throw }
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$serverProcess = Start-Process -FilePath $pythonPath -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$Port") -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimePath 'server.stdout.log') -RedirectStandardError (Join-Path $runtimePath 'server.stderr.log') -PassThru
@{process_id=$serverProcess.Id; port=$Port; executable=$pythonPath} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtimePath 'server.json') -Encoding utf8
for ($attempt = 0; $attempt -lt 25; $attempt++) {
    Start-Sleep -Milliseconds 200
    try {
        $health = Invoke-RestMethod -Uri "$serverUrl/healthz" -TimeoutSec 1
        if ($health.name -eq 'ai-call-g2') {
            Write-Output "Ready: $serverUrl"
            if ($OpenBrowser) { Start-Process $serverUrl }
            exit 0
        }
    } catch {}
    if ($serverProcess.HasExited) { break }
}
throw "Server did not start. See runtime\server.stderr.log."
