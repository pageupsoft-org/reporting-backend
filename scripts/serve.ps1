# Windows: seed the demo (first run) and serve the API + built UI on http://127.0.0.1:8000
#   powershell -ExecutionPolicy Bypass -File scripts\serve.ps1 [-Build] [-Reset] [-Port 8000]
param([switch]$Build, [switch]$Reset, [int]$Port = 8000)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
# Generous timeout: some networks stall ~15 s on IPv6 before falling back to IPv4.
if (-not $env:UV_HTTP_TIMEOUT) { $env:UV_HTTP_TIMEOUT = "180" }
uv sync
if ($LASTEXITCODE -ne 0) {
    if (-not (Test-Path ".venv\Scripts\python.exe")) { exit $LASTEXITCODE }
    Write-Warning "uv sync failed (network?); starting with the existing environment."
    $env:UV_NO_SYNC = "1"
}
if ($Build -or -not (Test-Path "frontend\dist\index.html")) {
    Push-Location frontend
    npm ci; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    npm run build; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Pop-Location
}
$seedArgs = @(); if ($Reset) { $seedArgs += "--reset" }
uv run python scripts/seed_demo.py @seedArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
# Fail early with a clear message if the port is taken (e.g. another server, or one in WSL).
$probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
try { $probe.Start(); $probe.Stop() }
catch {
    Write-Host "Port $Port is already in use. Stop the other server, or run: scripts\serve.ps1 -Port 8080" -ForegroundColor Yellow
    exit 1
}
Write-Host "Open http://127.0.0.1:$Port" -ForegroundColor Green
uv run uvicorn reporting.app.main:app --host 127.0.0.1 --port $Port
