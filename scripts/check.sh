#!/usr/bin/env bash
# Everything CI runs: lint, module boundaries, types, tests, front-end typecheck.
set -euo pipefail
cd "$(dirname "$0")/.."
uv run ruff check reporting tests scripts
uv run lint-imports
uv run mypy reporting
uv run pytest
(cd frontend && npm run typecheck)
