$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'server-process.ps1')
$recordPath = Join-Path $projectRoot 'runtime\server.json'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'

Invoke-WithServerLock $projectRoot {
    if (-not (Test-Path -LiteralPath $recordPath)) { Write-Output 'No server record.'; return }
    $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
    Stop-RecordedServer $record $pythonPath
    Remove-Item -LiteralPath $recordPath
    Write-Output 'AI Call G2 stopped.'
}
