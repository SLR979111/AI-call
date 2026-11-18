$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    throw 'Install Ollama from https://ollama.com/download first.'
}
& uv run --locked python -m app.local_provider
if ($LASTEXITCODE -ne 0) { throw 'Local speech model installation failed.' }
$env:OLLAMA_HOST = '127.0.0.1:11434'
& ollama pull qwen3:4b-instruct-2507-q4_K_M
if ($LASTEXITCODE -ne 0) { throw 'Start Ollama and retry model installation.' }
Write-Output 'Offline models ready. Restart AI Call and select local mode in Task settings.'
