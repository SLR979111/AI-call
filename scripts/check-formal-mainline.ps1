# Read-only formal mainline check. Never restarts 8081/8091/8087.
$ErrorActionPreference = 'Continue'

Write-Host '=== 8081 app container ==='
docker ps --filter 'name=aicall-app-1' --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}'
docker inspect aicall-app-1 --format 'image={{.Image}} created={{.Created}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'

Write-Host '=== health ==='
try {
    $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8081/healthz' -TimeoutSec 5
    Write-Host "8081=$($r.StatusCode)"
} catch {
    Write-Host "8081=DOWN $($_.Exception.Message)"
}
try {
    $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8087/healthz' -TimeoutSec 5
    Write-Host "8087=$($r.StatusCode)"
} catch {
    Write-Host "8087=DOWN $($_.Exception.Message)"
}

Write-Host '=== 8091 ==='
$c = Get-NetTCPConnection -LocalPort 8091 -State Listen -ErrorAction SilentlyContinue
if ($c) { Write-Host '8091=LISTENING' } else { Write-Host '8091=NOT_LISTENING' }

Write-Host '=== git ==='
git rev-parse --short HEAD
git status --short
