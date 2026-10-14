#!/usr/bin/env bash
# Re-runs the external-audit repro battery against the fixed tree.
# Run:  bash docs/verification/proofs/audit_2026-10-13/run_battery.sh | tee docs/verification/proofs/audit_2026-10-13/battery_after_fix.log
# Conventions:
#   * Python scripts: repo venv, scratch HOME + empty MACHINA_ENV_FILE,
#     MACHINA_TEST_DATABASE_URL -> local machina_test.
#   * Node scripts: real UI modules via absolute imports.
#   * DB repros write/clean only rows in machina_test.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO=/Users/shadow/machina
SCRATCH="${TMPDIR:-/tmp}/opencode/audit_battery"
mkdir -p "$SCRATCH/home"
: > "$SCRATCH/empty.env"

export HOME="$SCRATCH/home"
export MACHINA_ENV_FILE="$SCRATCH/empty.env"
export MACHINA_TEST_DATABASE_URL="${MACHINA_TEST_DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/machina_test}"

run_one() {
  local f="$1"; shift
  echo "===== $(basename "$f") ====="
  "$@" >"$SCRATCH/out" 2>&1
  local code=$?
  tail -14 "$SCRATCH/out"
  echo "exit: $code"
}

for f in "$HERE"/audit_*.py; do
  run_one "$f" "$REPO/.venv/bin/python" "$f"
done

for f in "$HERE"/audit_*.mjs; do
  run_one "$f" node "$f"
done

for f in "$HERE"/audit_*.sh; do
  run_one "$f" bash "$f"
done
