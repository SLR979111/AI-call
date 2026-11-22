# Serialize the start/stop record for this checkout, including concurrent clicks.
function Invoke-WithServerLock([string]$Root, [scriptblock]$Action) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes([IO.Path]::GetFullPath($Root).ToUpperInvariant())
        $key = [BitConverter]::ToString($hash.ComputeHash($bytes)).Replace('-', '')
    } finally { $hash.Dispose() }
    $mutex = [Threading.Mutex]::new($false, "Local\ai-call-g2-$key")
    try {
        try { [void]$mutex.WaitOne() } catch [Threading.AbandonedMutexException] {}
        try { & $Action } finally { $mutex.ReleaseMutex() }
    } finally { $mutex.Dispose() }
}

function Stop-RecordedServer($Record, [string]$PythonPath) {
    $processId = [int]$Record.process_id
    $portPattern = "--port $([int]$Record.port)(\s|$)"
    $running = Get-CimInstance Win32_Process -Filter "ProcessId = $processId"
    if (-not $running) { return }
    $started = $running.CreationDate.ToUniversalTime().Ticks
    if ($running.ExecutablePath -ne $PythonPath -or
        $running.CommandLine -notmatch 'uvicorn app\.main:app' -or
        $running.CommandLine -notmatch $portPattern -or
        ($Record.started_utc_ticks -and $started -ne [long]$Record.started_utc_ticks)) {
        throw 'The recorded process does not match this server.'
    }
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $processId" | Where-Object {
        $_.CommandLine -match 'uvicorn app\.main:app' -and $_.CommandLine -match $portPattern
    }
    foreach ($child in $children) { Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue }
    Stop-Process -Id $processId -ErrorAction SilentlyContinue
}
