# Windows: API with auto-reload on :8000 and the Vite dev server on :5173 (open the latter).
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
if (-not (Test-Path "data\demo")) { uv run python scripts/seed_demo.py }
$api = Start-Process uv -ArgumentList "run uvicorn reporting.app.main:app --reload --port 8000" -NoNewWindow -PassThru
try {
    Push-Location frontend
    if (-not (Test-Path "node_modules\.bin\vite.cmd")) { npm ci }
    npm run dev
} finally {
    Pop-Location
    Stop-Process -Id $api.Id -ErrorAction SilentlyContinue
}
