# Production build (Windows): Python deps, front end, and the full check suite.
#   powershell -ExecutionPolicy Bypass -File scripts\build.ps1 [-SkipChecks]
param([switch]$SkipChecks)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not $env:UV_HTTP_TIMEOUT) { $env:UV_HTTP_TIMEOUT = "180" }

function Step($name, [scriptblock]$cmd) {
    Write-Host "==> $name" -ForegroundColor Cyan
    & $cmd
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $name" -ForegroundColor Red; exit $LASTEXITCODE }
}

Step "Python dependencies (locked)" { uv sync --frozen }
Push-Location frontend
Step "Front-end dependencies (locked)" { npm ci }
Step "Front-end type check" { npm run typecheck }
Step "Front-end build" { npm run build }
Pop-Location
if (-not $SkipChecks) {
    Step "Lint" { uv run ruff check reporting tests scripts }
    Step "Module boundaries" { uv run lint-imports }
    Step "Type check" { uv run mypy reporting }
    Step "Tests" { uv run pytest -q }
}
Write-Host "Build complete: frontend\dist is ready. Start with scripts\run-production.ps1" -ForegroundColor Green
