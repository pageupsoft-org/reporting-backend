#!/usr/bin/env bash
# Production build (Linux/macOS): Python deps, front end, and the full check suite.
#   scripts/build.sh [--skip-checks]
set -euo pipefail
cd "$(dirname "$0")/.."
echo "==> Python dependencies (locked)"; uv sync --frozen
echo "==> Front end"; (cd frontend && npm ci && npm run typecheck && npm run build)
if [[ "${1:-}" != "--skip-checks" ]]; then
  echo "==> Checks"
  uv run ruff check reporting tests scripts
  uv run lint-imports
  uv run mypy reporting
  uv run pytest -q
fi
echo "Build complete: frontend/dist is ready. Start with scripts/run-production.sh"
