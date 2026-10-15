#!/usr/bin/env bash
# R4-095 probe: wheel telemetry exclusion (bullet g).
# RED: wheel built from the pre-fix pyproject (HEAD worktree) with the real
# runtime telemetry file present -> both telemetry members ship.
# GREEN: wheel built from the working tree -> members are gone and the rest of
# the skills tree is byte-for-byte the same set.
# Also proves tools/check_release_artifacts.py now fails the pre-fix wheel.
set -u

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
OUTDIR="$(mktemp -d "${TMPDIR:-/tmp}/r4095-wheel.XXXXXX")"
WT="$OUTDIR/prefix-worktree"
cleanup() {
  git -C "$REPO" worktree remove --force "$WT" 2>/dev/null || true
  rm -rf "$OUTDIR"
}
trap cleanup EXIT

FAILURES=0
check() { # check <label> <actual> <expected>
  if [ "$2" = "$3" ]; then
    printf 'PASS %-52s = %s\n' "$1" "$2"
  else
    printf 'FAIL %-52s = %s (expected %s)\n' "$1" "$2" "$3"
    FAILURES=$((FAILURES + 1))
  fi
}

list_wheel() { # list_wheel <wheel> -> telemetry count, skills count, member set file
  local wheel="$1" setfile="$2"
  PYTHONPATH= "$REPO/.venv/bin/python" - "$wheel" "$setfile" <<'PY'
import sys
import zipfile
wheel, setfile = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(wheel) as archive:
    names = sorted(archive.namelist())
telemetry = [n for n in names if n.startswith("skills/telemetry.json")]
skills = [n for n in names if n.startswith("skills/")]
with open(setfile, "w") as fh:
    fh.write("\n".join(names) + "\n")
print(len(telemetry), len(skills), sep=" ")
PY
}

printf '== pre-fix checkout (git worktree --detach HEAD) ==\n'
git -C "$REPO" worktree add --detach "$WT" HEAD >/dev/null 2>&1
# The runtime telemetry file is gitignored state; copy it in so the pre-fix
# force-include actually has something to leak (it exists on this machine).
cp "$REPO/skills/telemetry.json" "$REPO/skills/telemetry.json.lock" "$WT/skills/"
PRE_OUT="$OUTDIR/pre"
mkdir -p "$PRE_OUT"
(cd "$WT" && "$REPO/.venv/bin/python" -m hatchling build -t wheel -d "$PRE_OUT") >/dev/null
PRE_WHEEL="$(ls "$PRE_OUT"/*.whl)"
printf 'pre-fix wheel: %s\n' "$(basename "$PRE_WHEEL")"
read -r PRE_TELEMETRY PRE_SKILLS <<<"$(list_wheel "$PRE_WHEEL" "$OUTDIR/pre.set")"
check "pre-fix telemetry members" "$PRE_TELEMETRY" "2"
printf 'pre-fix telemetry members: %s\n' \
  "$(grep '^skills/telemetry.json' "$OUTDIR/pre.set" | tr '\n' ' ')"

printf '\n== post-fix working tree ==\n'
POST_OUT="$OUTDIR/post"
mkdir -p "$POST_OUT"
(cd "$REPO" && "$REPO/.venv/bin/python" -m hatchling build -t wheel -d "$POST_OUT") >/dev/null
POST_WHEEL="$(ls "$POST_OUT"/*.whl)"
printf 'post-fix wheel: %s\n' "$(basename "$POST_WHEEL")"
read -r POST_TELEMETRY POST_SKILLS <<<"$(list_wheel "$POST_WHEEL" "$OUTDIR/post.set")"
check "post-fix telemetry members" "$POST_TELEMETRY" "0"
check "post-fix skills member count (pre minus telemetry)" "$POST_SKILLS" "$((PRE_SKILLS - 2))"
grep -v '^skills/telemetry.json' "$OUTDIR/pre.set" > "$OUTDIR/pre.filtered"
if diff -q "$OUTDIR/pre.filtered" "$OUTDIR/post.set" >/dev/null; then
  printf 'PASS %-52s = identical\n' "member sets differ only by telemetry"
else
  printf 'FAIL %-52s\n' "member sets differ only by telemetry"
  diff "$OUTDIR/pre.filtered" "$OUTDIR/post.set" | head -20
  FAILURES=$((FAILURES + 1))
fi

printf '\n== artifact checker on the pre-fix wheel (RED) ==\n'
if "$REPO/.venv/bin/python" "$REPO/tools/check_release_artifacts.py" "$PRE_WHEEL" \
  >"$OUTDIR/pre_check.log" 2>&1; then
  printf 'FAIL %-52s = checker accepted leaked telemetry\n' "pre-fix wheel rejected"
  FAILURES=$((FAILURES + 1))
else
  grep -m1 'telemetry leaked' "$OUTDIR/pre_check.log" | sed 's/^/  /'
  check "pre-fix wheel rejected for telemetry" \
    "$([ "$(grep -c 'telemetry leaked' "$OUTDIR/pre_check.log")" -ge 1 ] && echo yes)" "yes"
fi

printf '\n== artifact checker on the post-fix wheel (GREEN) ==\n'
if "$REPO/.venv/bin/python" "$REPO/tools/check_release_artifacts.py" "$POST_WHEEL" \
  >"$OUTDIR/post_check.log" 2>&1; then
  tail -n1 "$OUTDIR/post_check.log" | sed 's/^/  /'
  check "post-fix wheel accepted" "yes" "yes"
else
  printf 'FAIL %-52s\n' "post-fix wheel accepted"
  tail -5 "$OUTDIR/post_check.log"
  FAILURES=$((FAILURES + 1))
fi

printf '\n== result: '
if [ "$FAILURES" = "0" ]; then
  printf 'ALL PROBES PASS\n'
  exit 0
fi
printf '%s FAILURE(S)\n' "$FAILURES"
exit 1
