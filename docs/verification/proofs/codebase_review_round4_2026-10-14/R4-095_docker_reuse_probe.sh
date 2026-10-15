#!/usr/bin/env bash
# R4-095 probe: start_docker_db credential reconciliation and the rc=2
# "do not fall back" contract. Sources the real tools/install-helpers.sh and
# drives it with a fake `docker` (no real containers, no network, no installs).
set -u

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
LIB="$REPO/tools/install-helpers.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/r4095-docker-probe.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

log() { printf 'LOG: %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die() { printf 'DIE: %s\n' "$*" >&2; return 99; }
rand_hex() { printf 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef'; }
# shellcheck source=/dev/null
. "$LIB"

FAKEBIN="$WORK/bin"
mkdir -p "$FAKEBIN"
cat > "$FAKEBIN/docker" <<'FAKE'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >> "$DOCKER_LOG"
case "$1 $2" in
  "ps --format") [ "$CONTAINER" = "running" ] && printf 'machina-db\n'; exit 0 ;;
  "ps -a") [ "$CONTAINER" != "absent" ] && printf 'machina-db\n'; exit 0 ;;
  "inspect --format") [ -n "${CONTAINER_PASSWORD:-}" ] && printf 'POSTGRES_PASSWORD=%s\n' "$CONTAINER_PASSWORD"; exit 0 ;;
esac
exit 0
FAKE
chmod +x "$FAKEBIN/docker"
PATH="$FAKEBIN:$PATH"
export PATH

FAILURES=0

check() { # check <label> <actual> <expected>
  if [ "$2" = "$3" ]; then
    printf 'PASS %-42s = %s\n' "$1" "$2"
  else
    printf 'FAIL %-42s = %s (expected %s)\n' "$1" "$2" "$3"
    FAILURES=$((FAILURES + 1))
  fi
}

scenario() { # scenario <name> <container:absent|running|stopped> <password> <env_url>
  local name="$1" container="$2" cpass="$3" envurl="$4"
  ENV_FILE="$WORK/$name.env"
  printf 'DATABASE_URL=%s\n' "$envurl" > "$ENV_FILE"
  BEFORE_ENV="$(cat "$ENV_FILE")"
  DOCKER_LOG="$WORK/$name.docker.log"
  : > "$DOCKER_LOG"
  CONTAINER="$container" CONTAINER_PASSWORD="$cpass" DOCKER_LOG="$DOCKER_LOG"
  export CONTAINER CONTAINER_PASSWORD DOCKER_LOG
  RC=0
  OUT="$(start_docker_db 2>&1)" || RC=$?
  AFTER_ENV="$(cat "$ENV_FILE")"
  CALLS="$(cat "$DOCKER_LOG")"
  printf '\n--- %s (container=%s)\n%s\n' "$name" "$container" "$OUT"
  printf 'rc=%s | DATABASE_URL now: %s\n' "$RC" "$(grep '^DATABASE_URL=' "$ENV_FILE")"
}

printf '== probe 1: running container, password matches DATABASE_URL ==\n'
scenario reuse_match running abc123 "postgresql://postgres:abc123@127.0.0.1:5432/machina"
check "reuse_match rc" "$RC" "0"
check "reuse_match env untouched" "$AFTER_ENV" "$BEFORE_ENV"
check "reuse_match no run/start" "$(grep -c -E 'docker (run|start) ' <<<"$CALLS" || true)" "0"

printf '\n== probe 2: running container, installer-default URL -> reconciled ==\n'
scenario reconcile_default running containerpass "postgresql://postgres:postgres@localhost:5432/machina"
check "reconcile_default rc" "$RC" "0"
check "reconcile_default env rewritten" \
  "$(grep '^DATABASE_URL=' "$ENV_FILE")" \
  "DATABASE_URL=postgresql://postgres:containerpass@127.0.0.1:5432/machina"

printf '\n== probe 3: running container, custom URL mismatch -> rc 2, no writes ==\n'
scenario conflict_custom running containerpass "postgresql://postgres:otherpass@127.0.0.1:5432/machina"
check "conflict_custom rc" "$RC" "2"
check "conflict_custom env untouched" "$AFTER_ENV" "$BEFORE_ENV"
check "conflict_custom no run/start" "$(grep -c -E 'docker (run|start) ' <<<"$CALLS" || true)" "0"
check "conflict_custom guidance names both fixes" "$(grep -c -E 'fix A|fix B' <<<"$OUT" || true)" "2"
check "conflict_custom does not echo password" "$(grep -c 'containerpass' <<<"$OUT" || true)" "0"

printf '\n== probe 4: container credentials unreadable -> rc 2 ==\n'
scenario unreadable running "" "postgresql://postgres:anything@127.0.0.1:5432/machina"
check "unreadable rc" "$RC" "2"
check "unreadable env untouched" "$AFTER_ENV" "$BEFORE_ENV"

printf '\n== probe 5: stopped container, matching password -> started ==\n'
scenario stopped_match stopped abc123 "postgresql://postgres:abc123@127.0.0.1:5432/machina"
check "stopped_match rc" "$RC" "0"
check "stopped_match docker start called" "$(grep -c 'docker start machina-db' <<<"$CALLS" || true)" "1"

printf '\n== probe 6: fresh create uses the pinned digest ==\n'
scenario fresh_create absent "" "postgresql://postgres:postgres@localhost:5432/machina"
check "fresh_create rc" "$RC" "0"
check "fresh_create docker run used pinned image" \
  "$(grep -c 'pgvector/pgvector:pg16@sha256:7b822b0aac60967beb1ea5e576b8602c94c300a157d187f385ae3e0da199b90a' <<<"$CALLS" || true)" "1"

printf '\n== install.sh rc=2 contract (static) ==\n'
INSTALL="$REPO/install.sh"
RC2_LINE="$(grep -n 'DOCKER_DB_RC" = "2"' "$INSTALL" | head -n 1 | cut -d: -f1)"
BREW_LINE="$(grep -n 'elif start_brew_db' "$INSTALL" | head -n 1 | cut -d: -f1)"
check "install.sh has rc=2 arm" "$([ -n "$RC2_LINE" ] && echo yes)" "yes"
check "rc=2 arm precedes brew fallback" \
  "$([ -n "$RC2_LINE" ] && [ -n "$BREW_LINE" ] && [ "$RC2_LINE" -lt "$BREW_LINE" ] && echo yes)" "yes"
check "rc=2 arm documents skipped brew" \
  "$(grep -c 'no brew install is attempted' "$INSTALL")" "1"

printf '\n== result: '
if [ "$FAILURES" = "0" ]; then
  printf 'ALL PROBES PASS\n'
  exit 0
fi
printf '%s FAILURE(S)\n' "$FAILURES"
exit 1
