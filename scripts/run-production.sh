#!/usr/bin/env bash
# Run in production mode (several worker processes, settings from .env). See DEPLOYMENT.md.
#   PORT=8000 REPORTING_WORKERS=4 scripts/run-production.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_FILE="${REPORTING_ENV_FILE:-.env}"
if [[ -f "$ENV_FILE" ]]; then export REPORTING_ENV_FILE="$(realpath "$ENV_FILE")"; fi
export REPORTING_ENV="${REPORTING_ENV:-production}" UV_NO_SYNC=1
[[ -f frontend/dist/index.html ]] || { echo "Front end not built: run scripts/build.sh"; exit 1; }
uv run python -c "from reporting.app.config import Settings; Settings.from_env(); print('Configuration OK')"
exec uv run uvicorn reporting.app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" \
  --workers "${REPORTING_WORKERS:-4}" --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
  --no-access-log --timeout-graceful-shutdown 30
