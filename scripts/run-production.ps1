# Run in production mode on Windows (several worker processes, settings from .env).
#   powershell -ExecutionPolicy Bypass -File scripts\run-production.ps1 [-Port 8000] [-Workers 4] [-EnvFile .env]
# Put it behind IIS/nginx/a load balancer that terminates HTTPS. See DEPLOYMENT.md.
param([int]$Port = 8000, [int]$Workers = 0, [string]$EnvFile = ".env", [string]$BindHost = "127.0.0.1")
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path $EnvFile)) {
    Write-Host "Missing $EnvFile. Copy .env.example to .env and fill it in (python scripts\generate_secrets.py)." -ForegroundColor Red
    exit 1
}
$env:REPORTING_ENV_FILE = (Resolve-Path $EnvFile).Path
if (-not $env:REPORTING_ENV) { $env:REPORTING_ENV = "production" }
if ($Workers -le 0) { $Workers = if ($env:REPORTING_WORKERS) { [int]$env:REPORTING_WORKERS } else { [Math]::Min(4, [Environment]::ProcessorCount) } }
if (-not (Test-Path "frontend\dist\index.html")) {
    Write-Host "The front end is not built. Run scripts\build.ps1 first." -ForegroundColor Red
    exit 1
}
$env:UV_NO_SYNC = "1"   # production runs the environment built by scripts\build.ps1

$probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse($BindHost), $Port)
try { $probe.Start(); $probe.Stop() } catch { Write-Host "Port $Port is already in use." -ForegroundColor Yellow; exit 1 }

# Validate configuration once, with readable errors, before starting workers.
uv run python -c "from reporting.app.config import Settings; Settings.from_env(); print('Configuration OK')"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Starting $Workers workers on http://${BindHost}:$Port" -ForegroundColor Green
uv run uvicorn reporting.app.main:app --host $BindHost --port $Port --workers $Workers `
    --proxy-headers --forwarded-allow-ips "127.0.0.1" --no-access-log --timeout-graceful-shutdown 30
