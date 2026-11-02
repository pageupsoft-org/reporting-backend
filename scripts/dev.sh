#!/usr/bin/env bash
# Start the API (port 8000, auto-reload) and the Vite dev server (port 5173) together.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d data/demo ] || uv run python scripts/seed_demo.py
trap 'kill 0' EXIT
uv run uvicorn reporting.app.main:app --reload --port 8000 &
(cd frontend && npm run dev) &
wait
