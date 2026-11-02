#!/usr/bin/env bash
# Usage: scripts/mark.sh T4 T5 ...  — tick tasks in TASKS.md
cd "$(dirname "$0")/.."
for t in "$@"; do sed -i "s/- \[ \] \*\*$t\./- [x] **$t./" TASKS.md; done
grep -E "^\- \[x\]" TASKS.md | wc -l | xargs -I{} echo "{} tasks complete"
