# Stop only the experimental 8087 service. Never touches 8081/8091.
$ErrorActionPreference = 'Stop'
$conn = Get-NetTCPConnection -LocalPort 8087 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $conn) {
    Write-Host '8087 is not listening; nothing to stop.'
    exit 0
}
$pidToStop = [int]$conn.OwningProcess
$proc = Get-Process -Id $pidToStop -ErrorAction SilentlyContinue
Write-Host ("Stopping 8087: PID={0} Name={1}" -f $pidToStop, ($proc.ProcessName))
Stop-Process -Id $pidToStop -Force
Start-Sleep -Seconds 1
if (Get-NetTCPConnection -LocalPort 8087 -State Listen -ErrorAction SilentlyContinue) {
    Write-Error '8087 is still listening; stop it manually after checking the owner.'
    exit 1
}
Write-Host '8087 stopped. 8081/8091 were not touched.'
