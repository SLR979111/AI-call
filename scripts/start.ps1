param([ValidateRange(1, 65535)][int]$Port = 8088, [switch]$OpenBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
. (Join-Path $PSScriptRoot 'server-process.ps1')
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runtimePath = Join-Path $projectRoot 'runtime'
$recordPath = Join-Path $runtimePath 'server.json'
$serverUrl = "http://127.0.0.1:$Port"

Invoke-WithServerLock $projectRoot {
    New-Item -ItemType Directory -Force -Path $runtimePath | Out-Null
    $existing = $null
    try {
        $existing = Invoke-RestMethod -Uri "$serverUrl/healthz" -TimeoutSec 2
    } catch {
        if ($_.Exception.Response) { throw "Port $Port belongs to another application." }
    }
    if ($null -ne $existing) {
        if ($existing.name -ne 'ai-call-g2') { throw "Port $Port belongs to another application." }
        Write-Output "Already running: $serverUrl"
        return
    }

    & uv sync --locked --group dev
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $pythonPath -m app.local_asr
    if ($LASTEXITCODE -ne 0) { Write-Warning 'First-pass ASR model unavailable. Continuing with cloud ASR.' }
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $arguments = @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$Port")
    $server = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimePath 'server.stdout.log') -RedirectStandardError (Join-Path $runtimePath 'server.stderr.log') -PassThru
    $record = @{process_id=$server.Id; port=$Port; executable=$pythonPath}
    $pendingPath = Join-Path $runtimePath "server-$($server.Id).tmp"
    try {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($server.Id)"
        if ($owner) { $record.started_utc_ticks = $owner.CreationDate.ToUniversalTime().Ticks.ToString() }
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while (-not $server.HasExited -and [DateTime]::UtcNow -lt $deadline) {
            $health = $null
            try { $health = Invoke-RestMethod -Uri "$serverUrl/healthz" -TimeoutSec 1 } catch {}
            if ($health.name -eq 'ai-call-g2' -and -not $server.HasExited) {
                $record | ConvertTo-Json | Set-Content -LiteralPath $pendingPath -Encoding utf8
                if ([IO.File]::Exists($recordPath)) {
                    [IO.File]::Replace($pendingPath, $recordPath, [System.Management.Automation.Language.NullString]::Value)
                } else {
                    [IO.File]::Move($pendingPath, $recordPath)
                }
                Write-Output "Ready: $serverUrl"
                return
            }
            Start-Sleep -Milliseconds 200
        }
        throw 'Server did not start. See runtime\server.stderr.log.'
    } catch {
        if (-not $server.HasExited) { Stop-RecordedServer $record $pythonPath }
        throw
    } finally {
        if ([IO.File]::Exists($pendingPath)) { Remove-Item -LiteralPath $pendingPath }
        $server.Dispose()
    }
}
if ($OpenBrowser) { Start-Process $serverUrl }
