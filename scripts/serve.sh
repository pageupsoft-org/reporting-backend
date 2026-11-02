#!/usr/bin/env bash
# Build the front end and serve everything from one process on port 8000.
set -euo pipefail
cd "$(dirname "$0")/.."
(cd frontend && npm ci && npm run build)
[ -d data/demo ] || uv run python scripts/seed_demo.py
exec uv run uvicorn reporting.app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
