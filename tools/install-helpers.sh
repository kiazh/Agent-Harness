# lib/install-helpers.sh — testable installer primitives.
#
# Sourcing this file has no side effects (function definitions only), so
# tests can load it directly without running the installer. Callers must
# provide log()/warn(); nothing here calls die() — every failure is a
# nonzero return, and no configuration is half-written on failure.
#
# Conventions shared with install.sh:
#   - The env file target is resolved ONCE (resolve_env_file) and every
#     read/write/chmod goes through env_get/env_set against that target.
#   - Writes are atomic same-directory replacements with mode 0600.
#   - Secrets are never echoed; hidden prompting is all-or-nothing.

# Installer-owned DATABASE_URL placeholder. Only an unset URL or exactly
# this value may be replaced with generated credentials — anything else is
# a custom URL the installer must preserve.
INSTALLER_DEFAULT_DB_URL="postgresql://postgres:postgres@localhost:5432/machina"

# Keep curl|bash and CI logs plain. On a real terminal, show progress only
# while a real command is running; always reveal its output if it fails.
run_animated() (
  spinner_label="$1"
  spinner_tick=0
  shift
  if [ ! -t 1 ] || [ -n "${NO_COLOR:-}" ] || [ -n "${CI:-}" ] || [ "${TERM:-dumb}" = "dumb" ]; then
    log "$spinner_label"
    "$@"
    exit $?
  fi
  spinner_output_file="$(mktemp "${TMPDIR:-/tmp}/machina-install.XXXXXX" 2>/dev/null)" || {
    log "$spinner_label"
    "$@"
    exit $?
  }
  chmod 600 "$spinner_output_file" || {
    rm -f "$spinner_output_file"
    log "$spinner_label"
    "$@"
    exit $?
  }
  "$@" >"$spinner_output_file" 2>&1 &
  spinner_pid=$!
  trap 'if [ -n "${spinner_pid:-}" ]; then kill "$spinner_pid" 2>/dev/null || true; wait "$spinner_pid" 2>/dev/null || true; fi; rm -f "$spinner_output_file"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  while kill -0 "$spinner_pid" 2>/dev/null; do
    case $((spinner_tick % 4)) in
      0) spinner_frame='◜' ;;
      1) spinner_frame='◝' ;;
      2) spinner_frame='◞' ;;
      *) spinner_frame='◟' ;;
    esac
    printf '\r\033[2K\033[36m[install]\033[0m %s %s' "$spinner_frame" "$spinner_label"
    spinner_tick=$((spinner_tick + 1))
    sleep 0.12
  done
  if wait "$spinner_pid"; then spinner_status=0; else spinner_status=$?; fi
  spinner_pid=""
  printf '\r\033[2K'
  if [ "$spinner_status" = "0" ]; then
    log "✓ $spinner_label"
    [ ! -s "$spinner_output_file" ] || tail -n 3 "$spinner_output_file"
  else
    warn "✗ $spinner_label failed"
    cat "$spinner_output_file" >&2
  fi
  exit "$spinner_status"
)

installer_next_steps() {
  local state="$1" docs="$2" step=2
  printf '\nMachina installed. Next, in this order:\n'
  printf '  1. machina setup            Configure your provider and database before opening the UI, chat, or server.\n'
  case "$state" in
    ready)
      printf '  Database schema initialized automatically.\n'
      printf '  If setup changes DATABASE_URL, run machina init again.\n'
      ;;
    skipped)
      printf '  Database schema was skipped (--skip-init).\n'
      printf '  2. machina init             Create the schema after setup.\n'
      step=3
      ;;
    *)
      printf '  Database schema is not ready.\n'
      printf '  2. machina init             Create the schema after fixing DATABASE_URL/PostgreSQL in setup.\n'
      step=3
      ;;
  esac
  printf '  %s. machina doctor           Check your configuration.\n' "$step"
  step=$((step + 1))
  printf '  %s. machina              Open the terminal UI.\n' "$step"
  printf '     machina chat "Hello"     Run a one-shot chat after setup.\n'
  printf '     machina serve            Start the HTTP API after setup.\n'
  printf '\nDocs: %s\n' "$docs"
}

# resolve_env_file <startup_dir> — stdout: absolute env-file path.
# MACHINA_ENV_FILE absolute wins as-is; relative MACHINA_ENV_FILE anchors to the
# startup directory (so a later cd into the checkout cannot redirect it);
# unset means .env in the current (checkout) directory.
resolve_env_file() {
  local startup_dir="$1" override="${MACHINA_ENV_FILE:-}"
  if [ -n "$override" ]; then
    case "$override" in
      /*) printf '%s' "$override" ;;
      *) printf '%s' "${startup_dir%/}/${override}" ;;
    esac
  else
    printf '%s' "$PWD/.env"
  fi
}

# env_get <key> [file] — stdout: value of KEY ("" when absent).
env_get() {
  local key="$1" file="${2:-.env}" line
  [ -f "$file" ] || return 0
  line="$(grep -E "^${key}=" "$file" 2>/dev/null | tail -n 1 || true)"
  [ -n "$line" ] || return 0
  printf '%s' "${line#*=}"
}

# env_set <key> <value> [file] — atomically set KEY=VALUE, mode 0600.
# Creates the file (and only the KEY line) when absent. Fails closed when
# the target directory is missing or unwritable, or the target is a
# directory. Never touches any other file.
env_set() {
  local key="$1" value="$2" file="${3:-.env}" dir tmp
  case "$key" in
    "" | *[!A-Za-z0-9_]* | [0-9]*) return 1 ;;
  esac
  dir="$(dirname "$file")"
  [ -d "$dir" ] || return 1
  [ -e "$file" ] && [ ! -f "$file" ] && return 1
  tmp="$(mktemp "${file}.tmp.XXXXXX" 2>/dev/null)" || return 1
  chmod 600 "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
  if [ -f "$file" ]; then
    grep -v -E "^${key}=" "$file" > "$tmp" 2>/dev/null || true
  fi
  printf '%s=%s\n' "$key" "$value" >> "$tmp" || { rm -f "$tmp"; return 1; }
  mv "$tmp" "$file" || { rm -f "$tmp"; return 1; }
  chmod 600 "$file" 2>/dev/null || true
  return 0
}

# db_url_is_installer_default [file] — true when DATABASE_URL is unset or
# exactly the installer-owned placeholder, i.e. generated credentials may
# replace it. Any other present value is custom and must be preserved.
db_url_is_installer_default() {
  local file="${1:-.env}" current
  current="$(env_get DATABASE_URL "$file")"
  [ -z "$current" ] && return 0
  [ "$current" = "$INSTALLER_DEFAULT_DB_URL" ] && return 0
  return 1
}

# maybe_write_db_url <url> [file] — replace DATABASE_URL only when it is
# unset or the installer-owned placeholder. Returns 0 and logs when the
# URL was written OR when a custom URL was deliberately preserved; returns
# 1 only when the write itself failed.
maybe_write_db_url() {
  local url="$1" file="${2:-.env}"
  if db_url_is_installer_default "$file"; then
    env_set DATABASE_URL "$url" "$file" || return 1
    log "Wrote generated Postgres credentials to DATABASE_URL in $file (mode 0600)"
  else
    warn "Reusing existing custom DATABASE_URL in $file; ensure it matches the new database if init fails"
  fi
  return 0
}

# --- Database provisioning (moved verbatim from install.sh, except the
# DATABASE_URL write decisions, which now go through maybe_write_db_url so
# custom URLs survive in every branch). These need the installer's globals
# (log/warn, MACHINA_BIN, OS_FAMILY) at CALL time only.
# shellcheck disable=SC2154

try_init() { "$MACHINA_BIN" init 2>&1; }

start_docker_db() {
  command -v docker >/dev/null 2>&1 || return 1
  # AH-029: `docker ps` lists running containers only — a stopped
  # machina-db would be missed and `docker run --name` would fail with
  # "already exists". Inspect all containers (-a) and reuse the named one.
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^machina-db$'; then
    log "machina-db already running, reusing it"
    return 0
  fi
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^machina-db$'; then
    log "Starting existing stopped machina-db container"
    docker start machina-db >/dev/null || return 1
    return 0
  fi
  # AH-006: never publish Postgres beyond loopback with weak default
  # credentials. Bind 127.0.0.1 only and generate a random password, keeping
  # DATABASE_URL consistent. Existing custom DATABASE_URL values are left
  # alone for compatibility.
  DB_PASS="$(rand_hex | cut -c1-32)"
  DB_URL="postgresql://postgres:${DB_PASS}@127.0.0.1:5432/machina"
  log "Starting local Postgres (pgvector/pgvector:pg16 on 127.0.0.1:5432)"
  # shellcheck disable=SC2086
  docker run -d --name machina-db \
    -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD="$DB_PASS" -e POSTGRES_DB=machina \
    -p 127.0.0.1:5432:5432 pgvector/pgvector:pg16 >/dev/null || return 1
  # Point a default DATABASE_URL at the generated credentials; a custom URL
  # is preserved by maybe_write_db_url.
  maybe_write_db_url "$DB_URL" "$ENV_FILE" || return 1
  return 0
}

start_brew_db() {
  # macOS fallback when docker is unavailable: Homebrew PostgreSQL + pgvector.
  # Best-effort — if any step fails we return 1 and the caller prints manual fixes.
  # stdin is /dev/null so nothing here can block waiting on a piped install.
  [ "$OS_FAMILY" = "macos" ] || return 1
  command -v brew >/dev/null 2>&1 || return 1
  log "Installing PostgreSQL + pgvector via Homebrew (takes a few minutes)"
  brew install postgresql@16 pgvector < /dev/null || return 1
  brew services start postgresql@16 < /dev/null || return 1
  local pg_bin pg_user i extdir
  pg_bin="$(brew --prefix postgresql@16 2>/dev/null)/bin"
  [ -x "$pg_bin/pg_isready" ] || return 1
  log "Postgres: $("$pg_bin/pg_config" --version 2>/dev/null || echo unknown)"
  # The bottled pgvector is built against Homebrew's *unversioned* postgres —
  # whenever that major differs from @16, the extension files land in the
  # wrong tree and CREATE EXTENSION can never succeed. Detect it up front and
  # go straight to a source build instead of failing minutes later.
  extdir="$("$pg_bin/pg_config" --sharedir 2>/dev/null)/extension"
  if [ ! -f "$extdir/vector.control" ]; then
    warn "Brew pgvector wasn't built for postgresql@16; building pgvector from source"
    brew_vector_from_source "$pg_bin" || return 1
  fi
  for i in $(seq 1 30); do
    if "$pg_bin/pg_isready" -h localhost -p 5432 >/dev/null 2>&1; then break; fi
    [ "$i" = "30" ] && return 1
    sleep 2
  done
  pg_user="${USER:-$(whoami 2>/dev/null || echo postgres)}"
  # AH-AUDIT-040a: never reset an existing role's password as an automatic
  # repair — that would weaken credentials and break unrelated apps. Only a
  # role created here gets a generated password; pre-existing roles and
  # custom DATABASE_URL values are preserved untouched.
  CREATED_POSTGRES=0
  if ! "$pg_bin/psql" -h localhost -U "$pg_user" -d postgres -tc \
    "SELECT 1 FROM pg_roles WHERE rolname='machina'" 2>/dev/null | grep -q 1; then
    "$pg_bin/createuser" -h localhost -U "$pg_user" -s machina < /dev/null || return 1
    CREATED_POSTGRES=1
  fi
  if [ "$CREATED_POSTGRES" = "1" ]; then
    BREW_DB_PASS="$(rand_hex | cut -c1-32)"
    "$pg_bin/psql" -h localhost -U "$pg_user" -d postgres \
      -c "ALTER USER machina PASSWORD '${BREW_DB_PASS}';" >/dev/null 2>&1 || return 1
    # Unlike the pre-fix code, a custom DATABASE_URL is preserved even when
    # the role is new: generated credentials replace only an unset URL or
    # the documented installer-owned placeholder.
    maybe_write_db_url \
      "postgresql://machina:${BREW_DB_PASS}@localhost:5432/machina" \
      "$ENV_FILE" || return 1
  else
    warn "Reusing existing machina role; existing credentials preserved (set DATABASE_URL if init fails)"
  fi
  if ! "$pg_bin/psql" -h localhost -U "$pg_user" -d postgres -tc \
    "SELECT 1 FROM pg_database WHERE datname='machina'" 2>/dev/null | grep -q 1; then
    "$pg_bin/createdb" -h localhost -U "$pg_user" -O machina machina < /dev/null || return 1
  fi
  if ! "$pg_bin/psql" -h localhost -U machina -d machina \
    -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>&1; then
    warn "Brew pgvector doesn't fit postgresql@16; building pgvector from source"
    brew_vector_from_source "$pg_bin" || return 1
    "$pg_bin/psql" -h localhost -U machina -d machina \
      -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>&1 || return 1
  fi
  return 0
}

brew_vector_from_source() {
  # Compile pgvector against postgresql@16's pg_config when the bottle was
  # built for another PG major. Needs Xcode CLT (present if brew works) + git.
  local pg_bin="$1" build_dir
  command -v git >/dev/null 2>&1 || return 1
  command -v make >/dev/null 2>&1 || return 1
  build_dir="$(mktemp -d 2>/dev/null || echo /tmp/pgvector-build-$$)"
  (
    cd "$build_dir" \
      && git clone --depth 1 https://github.com/pgvector/pgvector.git < /dev/null \
      && cd pgvector \
      && make PG_CONFIG="$pg_bin/pg_config" < /dev/null \
      && make PG_CONFIG="$pg_bin/pg_config" install < /dev/null
  ) || return 1
  rm -rf "$build_dir"
  return 0
}

wait_for_init() {
  # Retry `machina init` while Postgres wakes up; on final failure print the real
  # error instead of swallowing it.
  local i out
  for i in $(seq 1 30); do
    if out="$(try_init 2>&1)"; then
      printf '%s\n' "$out"
      return 0
    fi
    sleep 2
  done
  printf '%s\n' "$out"
  return 1
}

# prompt_secret_hidden <prompt> — stdout: the typed value.
# Fails (no output, nonzero) unless hidden terminal input is fully
# established first: /dev/tty must open, stty state must be queryable, and
# echo must actually disable. The prompt prints only after hiding is on.
# Echo is restored on success, EOF, error, and INT/TERM/HUP/EXIT.
# Never falls back to visible typing.
prompt_secret_hidden() {
  local prompt="$1" stty_save answer
  [ -e /dev/tty ] || return 1
  stty_save="$(stty -g < /dev/tty 2>/dev/null)" || return 1
  [ -n "$stty_save" ] || return 1
  stty -echo < /dev/tty 2>/dev/null || return 1
  # Signals restore echo, disarm, then re-raise to die by the signal
  # (plain trap-return would re-block inside `read` on some shells).
  trap 'stty "$stty_save" < /dev/tty 2>/dev/null || true; trap - INT TERM HUP EXIT; kill -s TERM "$$"' INT TERM HUP
  trap 'stty "$stty_save" < /dev/tty 2>/dev/null || true' EXIT
  printf '%s' "$prompt" > /dev/tty 2>/dev/null || true
  if IFS= read -r answer < /dev/tty 2>/dev/null; then
    printf '\n' > /dev/tty 2>/dev/null || true
    stty "$stty_save" < /dev/tty 2>/dev/null || true
    trap - INT TERM HUP EXIT
    printf '%s' "$answer"
    return 0
  fi
  stty "$stty_save" < /dev/tty 2>/dev/null || true
  trap - INT TERM HUP EXIT
  return 1
}
