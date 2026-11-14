$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$recordPath = Join-Path $projectRoot 'runtime\server.json'
if (-not (Test-Path -LiteralPath $recordPath)) { Write-Output 'No server record.'; exit 0 }
$record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
$running = Get-CimInstance Win32_Process -Filter "ProcessId = $($record.process_id)"
if ($running) {
    if ($running.ExecutablePath -ne $record.executable -or $running.CommandLine -notmatch 'uvicorn app\.main:app' -or $running.CommandLine -notmatch "--port $($record.port)(\s|$)") {
        throw 'The recorded process is not this application. Nothing was stopped.'
    }
    # A Windows venv launcher may own a child CPython process. Stop only direct
    # children with the same verified app/port command, then the launcher.
    $serverChildren = Get-CimInstance Win32_Process -Filter "ParentProcessId = $($record.process_id)" | Where-Object {
        $_.CommandLine -match 'uvicorn app\.main:app' -and $_.CommandLine -match "--port $($record.port)(\s|$)"
    }
    foreach ($child in $serverChildren) { Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue }
    Stop-Process -Id $record.process_id -ErrorAction SilentlyContinue
    Write-Output 'AI Call G2 stopped.'
}
Remove-Item -LiteralPath $recordPath
