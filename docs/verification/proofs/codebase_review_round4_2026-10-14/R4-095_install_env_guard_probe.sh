#!/usr/bin/env bash
# R4-095 probe: install.sh MACHINA_ENV_FILE parent-directory guard.
# The guard block is extracted verbatim from install.sh between its markers and
# executed with controlled ENV_FILE values (the installer itself is never run).
# A pre-fix snippet shows the raw bash redirection error this guard replaces.
set -u

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
INSTALL="$REPO/install.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/r4095-envguard.XXXXXX")"
trap 'chmod 700 "$WORK" 2>/dev/null || true; rm -rf "$WORK"' EXIT

FAILURES=0
check() { # check <label> <actual> <expected>
  if [ "$2" = "$3" ]; then
    printf 'PASS %-46s = %s\n' "$1" "$2"
  else
    printf 'FAIL %-46s = %s (expected %s)\n' "$1" "$2" "$3"
    FAILURES=$((FAILURES + 1))
  fi
}

GUARD="$WORK/guard.sh"
sed -n '/--- env-file parent guard ---/,/--- end env-file parent guard ---/p' "$INSTALL" > "$GUARD"
printf '== extracted guard block (verbatim from install.sh) ==\n'
sed 's/^/  /' "$GUARD"
printf '\n'

run_guard() { # run_guard <env_file>
  local env_file="$1" out rc
  out="$(GUARD="$GUARD" ENV_FILE="$env_file" bash -c '
    die() { printf "DIE: %s\n" "$*" >&2; exit 99; }
    # shellcheck source=/dev/null
    . "$GUARD"
    printf "PROCEEDED\n"
  ' 2>&1)" && rc=0 || rc=$?
  printf '%s\nrc=%s\n' "$out" "$rc"
}

printf '== probe 1: parent directory missing ==\n'
OUT="$(run_guard "$WORK/missing-dir/.env")"
printf '%s\n' "$OUT"
check "missing parent rc" "$(tail -n1 <<<"$OUT")" "rc=99"
check "missing parent names the directory" "$(grep -c 'does not exist' <<<"$OUT")" "1"
check "missing parent is not a raw bash error" "$(grep -c 'No such file or directory' <<<"$OUT")" "0"

printf '\n== probe 2: parent directory not writable ==\n'
mkdir -p "$WORK/readonly"
chmod 555 "$WORK/readonly"
if [ -w "$WORK/readonly" ]; then
  printf 'SKIP (directory still writable — running privileged?)\n'
else
  OUT="$(run_guard "$WORK/readonly/.env")"
  printf '%s\n' "$OUT"
  check "readonly parent rc" "$(tail -n1 <<<"$OUT")" "rc=99"
  check "readonly parent names the directory" "$(grep -c 'is not writable' <<<"$OUT")" "1"
fi
chmod 700 "$WORK/readonly" 2>/dev/null || true

printf '\n== probe 3: valid parent proceeds ==\n'
mkdir -p "$WORK/fine"
OUT="$(run_guard "$WORK/fine/.env")"
printf '%s\n' "$OUT"
check "valid parent rc" "$(tail -n1 <<<"$OUT")" "rc=0"
check "valid parent proceeds" "$(grep -c PROCEEDED <<<"$OUT")" "1"

printf '\n== RED: pre-fix snippet hits the raw redirection error ==\n'
PREFIX_RC=0
PREFIX_OUT="$(ENV_FILE="$WORK/missing-dir/.env" bash -c '
  set -e
  [ -f "$ENV_FILE" ] || : > "$ENV_FILE"
' 2>&1)" || PREFIX_RC=$?
printf '%s\nrc=%s\n' "$PREFIX_OUT" "$PREFIX_RC"
check "pre-fix rc (raw failure)" "$PREFIX_RC" "1"
check "pre-fix emits raw bash error" "$(grep -c 'No such file or directory' <<<"$PREFIX_OUT")" "1"

printf '\n== static: guard precedes the .env redirection ==\n'
GUARD_LINE="$(grep -n 'end env-file parent guard' "$INSTALL" | head -n1 | cut -d: -f1)"
REDIRECT_LINE="$(grep -n '\[ -f "\$ENV_FILE" \] || : > "\$ENV_FILE"' "$INSTALL" | head -n1 | cut -d: -f1)"
check "guard marker found" "$([ -n "$GUARD_LINE" ] && echo yes)" "yes"
check "redirection line found" "$([ -n "$REDIRECT_LINE" ] && echo yes)" "yes"
check "guard runs before redirection" \
  "$([ "$GUARD_LINE" -lt "$REDIRECT_LINE" ] && echo yes)" "yes"

printf '\n== result: '
if [ "$FAILURES" = "0" ]; then
  printf 'ALL PROBES PASS\n'
  exit 0
fi
printf '%s FAILURE(S)\n' "$FAILURES"
exit 1
