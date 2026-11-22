# Promote the built candidate image to the local Docker app.
# Default is DRY RUN. Pass -Execute only inside a confirmed formal window.
param(
    [string]$CandidateTag = "aicall-app:candidate-8833132",
    [switch]$Execute
)
$ErrorActionPreference = "Stop"
$oldTag = "aicall-app:local"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupTag = "aicall-app:rollback-$stamp"

docker image inspect $CandidateTag --format "candidate={{.Id}} created={{.Created}}" | Out-Host
docker image inspect $oldTag --format "current={{.Id}} created={{.Created}}" | Out-Host
docker ps --filter "name=aicall-app-1" --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}" | Out-Host
curl.exe -sS -o NUL -w "8081_before=%{http_code}`n" http://127.0.0.1:8081/healthz

if (-not $Execute) {
    Write-Host ""
    Write-Host "DRY RUN ONLY. To switch in a formal window:"
    Write-Host "  .\scripts\promote-8081-candidate.ps1 -CandidateTag $CandidateTag -Execute"
    Write-Host "Planned actions:"
    Write-Host "  1. tag $oldTag -> $backupTag"
    Write-Host "  2. tag $CandidateTag -> $oldTag"
    Write-Host "  3. docker compose up -d app"
    Write-Host "  4. wait for 8081/healthz, rollback to $backupTag on failure"
    exit 0
}

$oldId = (docker image inspect $oldTag --format "{{.Id}}").Trim()
if (-not $oldId) { throw "No current $oldTag image to back up" }
docker tag $oldTag $backupTag
docker tag $CandidateTag $oldTag
try {
    docker compose up -d app
    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 2
        try {
            $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8081/healthz" -TimeoutSec 3
            if ($r.StatusCode -eq 200) { $ok = $true; break }
        } catch { }
    }
    if (-not $ok) { throw "8081 did not become healthy" }
    Write-Host "PROMOTED candidate=$CandidateTag backup=$backupTag"
} catch {
    Write-Warning "Promotion failed: $($_.Exception.Message). Rolling back to $backupTag"
    docker tag $backupTag $oldTag
    docker compose up -d app
    throw
}
