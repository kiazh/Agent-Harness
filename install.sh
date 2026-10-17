#!/usr/bin/env bash
# Machina zero-friction installer.
#
# Just works:
#   curl -fsSL https://raw.githubusercontent.com/kiazh/Machina-Harness/main/install.sh | bash
#
# With options (note the `--` after `-s` when piping via curl):
#   curl -fsSL https://raw.githubusercontent.com/kiazh/Machina-Harness/main/install.sh | bash -s -- --dir ~/machina --dev
#   curl -fsSL https://raw.githubusercontent.com/kiazh/Machina-Harness/main/install.sh | bash -s -- --interactive
#
# Local:
#   ./install.sh [--dir DIR] [--dev] [--no-ui] [--interactive] [--yes] [--with-docker-db|--no-docker-db] [--skip-init] [--interop MODE]
#
# What it does, in order:
#   1. Uses --dir / MACHINA_INSTALL_DIR when given; otherwise the current
#      checkout, or clones REPO_URL into ./machina
#   2. Checks python>=3.11, node>=22.19 (UI only), npm, git
#   3. Creates .venv, pip install -e . (or .[dev])
#   4. npm ci --prefix ui (unless --no-ui, Node <22.19, or npm absent)
#   5. Seeds .env from .env.example (any missing target, not only the default)
#      and generates random API/provenance/OAuth-master keys (never prompts
#      when piped unless --interactive and a terminal is available)
#   6. Tries `machina init`; on failure auto-provisions Postgres (docker, then
#      Homebrew on macOS unless --with-docker-db pinned it to Docker) and
#      retries. OS-aware: macos | windows | linux.
#   7. Installs an `machina` shim in ~/.local/bin, persists it to the shell rc
#      (~/.zshrc / ~/.bash_profile / ~/.bashrc), and on Windows registers the
#      venv in the user Path for PowerShell — so `machina` works in new shells.
#   8. Detects external agent harnesses (OpenCode, Codex, Hermes) and registers
#      Machina's MCP server with the chosen ones (--interop auto|all|none).
#   9. Tells the user to run `machina setup` before opening the UI, chat, or server.
#
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/kiazh/Machina-Harness.git}"
INSTALL_DIR="${MACHINA_INSTALL_DIR:-}"
DO_DEV=0
DO_UI=1
INTERACTIVE=0
ASSUME_YES=0
SKIP_INIT=0
DOCKER_DB="auto" # auto | yes | no
DB_AUTO="yes" # provision a local Postgres (docker, then Homebrew on macOS) when machina init fails
SHELL_RC="yes" # append ~/.local/bin to PATH in the shell rc file (opt out with --no-shell-rc)
INTEROP_MODE="${MACHINA_INTEROP:-auto}" # auto | all | none (external harness MCP registration)
PYTHON_BIN=""

log() {
  if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
    printf '\033[1;32m[install]\033[0m %s\n' "$*"
  else
    printf '[install] %s\n' "$*"
  fi
}
warn() {
  if [ -t 2 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
    printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2
  else
    printf '[warn] %s\n' "$*" >&2
  fi
}
die() { warn "$*"; exit 1; }

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  --dir DIR        Install directory (default: ./machina when cloning,
                   otherwise current directory if already a checkout)
  --dev            Install Python dev extras (pip install -e ".[dev]")
  --no-ui          Skip UI dependencies (npm install --prefix ui)
  --interactive    Prompt (via /dev/tty) for OPENROUTER_API_KEY and harness
                   interop even when piped via curl
  -y, --yes, --non-interactive
                   Never prompt; generate defaults and finish (default when piped)
  --with-docker-db Force auto-start of local Postgres via docker on init failure
  --no-docker-db   Never use docker for the DB
  --no-auto-db     Never auto-provision Postgres (docker or Homebrew); just warn
  --no-shell-rc    Don't touch shell rc files (~/.zshrc etc.); print PATH hint only
  --skip-init      Skip automatic `machina init` (DB schema creation)
  --interop MODE   External harness MCP registration: auto (default — prompt
                   on a TTY, otherwise print the detection summary), all
                   (register every detected OpenCode/Codex/Hermes), none (skip)
  -h, --help       Show this help

Env: REPO_URL, MACHINA_INSTALL_DIR, MACHINA_INTEROP (=--interop MODE)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) [ $# -ge 2 ] || die "--dir requires a value"; INSTALL_DIR="${2:-}"; shift 2 ;;
    --dev) DO_DEV=1; shift ;;
    --no-ui) DO_UI=0; shift ;;
    --interactive) INTERACTIVE=1; shift ;;
    -y|--yes|--non-interactive) ASSUME_YES=1; shift ;;
    --with-docker-db) DOCKER_DB="yes"; shift ;;
    --no-docker-db) DOCKER_DB="no"; shift ;;
    --no-auto-db) DB_AUTO="no"; shift ;;
    --no-shell-rc) SHELL_RC="no"; shift ;;
    --skip-init) SKIP_INIT=1; shift ;;
    --interop)
      [ $# -ge 2 ] || die "--interop requires a value (auto|all|none)"
      case "${2:-}" in
        auto | all | none) INTEROP_MODE="${2:-}" ;;
        *) die "--interop must be auto, all, or none (got '${2:-}')" ;;
      esac
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
done

# Piped via `curl | bash` => stdin is not a TTY => default to non-interactive.
if [ ! -t 0 ] && [ "$INTERACTIVE" = "0" ]; then
  ASSUME_YES=1
fi

# A bad MACHINA_INTEROP in the environment is a warning, not a fatal error:
# install continues and simply skips harness registration.
case "$INTEROP_MODE" in
  auto | all | none) ;;
  *)
    warn "MACHINA_INTEROP='$INTEROP_MODE' is not auto|all|none; skipping interop setup"
    INTEROP_MODE="none"
    ;;
esac

is_repo_dir() { [ -f "$1/pyproject.toml" ] && [ -d "$1/machina" ] && [ -d "$1/ui" ]; }

# clone_into_app_dir — clone REPO_URL into APP_DIR.
# F-33: operators may pre-create the target (e.g. to hold a pre-seeded .env
# before install.sh runs). `git clone` refuses a non-empty destination, so for
# that case clone into a temporary directory and merge it in without ever
# overwriting the operator's files. A normal empty/missing target keeps the
# plain one-step clone.
clone_into_app_dir() {
  if [ ! -d "$APP_DIR" ] || [ -z "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
    git clone --depth 1 "$REPO_URL" "$APP_DIR"
    return
  fi
  local tmp entry base
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/machina-clone.XXXXXX")" || return 1
  if ! git clone --depth 1 "$REPO_URL" "$tmp/repo"; then
    rm -rf "$tmp"
    return 1
  fi
  log "Target $APP_DIR is pre-populated; merging the clone without overwriting existing files"
  for entry in "$tmp/repo"/* "$tmp/repo"/.[!.]* "$tmp/repo"/..?*; do
    [ -e "$entry" ] || continue
    base="$(basename "$entry")"
    if [ -d "$entry" ] && [ -d "$APP_DIR/$base" ]; then
      cp -R -n "$entry"/. "$APP_DIR/$base"/ || { rm -rf "$tmp"; return 1; }
    elif [ -e "$APP_DIR/$base" ]; then
      warn "Keeping pre-existing $APP_DIR/$base (clone did not overwrite it)"
    else
      mv "$entry" "$APP_DIR/" || { rm -rf "$tmp"; return 1; }
    fi
  done
  rm -rf "$tmp"
}

pick_python() {
  local c
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      if [ "$("$c" -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)' 2>/dev/null || echo 0)" = "1" ]; then
        PYTHON_BIN="$c"
        return 0
      fi
    fi
  done
  return 1
}

rand_hex() { "$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(32))'; }
# Base64 of exactly 32 random bytes — the MCP OAuth master-key format.
rand_b64() { "$PYTHON_BIN" -c 'import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode("ascii"))'; }

# --- OS detection ------------------------------------------------------------
# OS_FAMILY: macos | windows | linux | other. Drives shell-rc selection
# (~/.zshrc vs ~/.bash_profile vs ~/.bashrc), the Windows PowerShell Path
# registration, and how Postgres gets auto-provisioned.
detect_os() {
  local kernel
  kernel="$(uname -s 2>/dev/null || echo unknown)"
  case "$kernel" in
    Darwin*) printf '%s' "macos" ;;
    MINGW* | MSYS* | CYGWIN*) printf '%s' "windows" ;;
    Linux*) printf '%s' "linux" ;;
    *) printf '%s' "other" ;;
  esac
}
OS_FAMILY="$(detect_os)"
log "Detected OS: $OS_FAMILY"

# --- Locate or clone the checkout -------------------------------------------
# F-04: an explicit --dir / MACHINA_INSTALL_DIR always wins. The current
# checkout is only the DEFAULT when no directory was requested.
if [ -z "$INSTALL_DIR" ] && is_repo_dir "$(pwd)"; then
  APP_DIR="$(pwd)"
  log "Using current checkout: $APP_DIR"
else
  if [ -z "$INSTALL_DIR" ]; then INSTALL_DIR="./machina"; fi
  APP_DIR="$INSTALL_DIR"
  if [ -d "$APP_DIR" ] && is_repo_dir "$APP_DIR"; then
    log "Using existing checkout: $APP_DIR"
    # Re-runs must pick up new code: fast-forward the checkout. Best-effort —
    # offline/diverged checkouts keep working with what's on disk.
    if git -C "$APP_DIR" pull --ff-only 2>/dev/null; then
      log "Checkout updated to latest"
    else
      warn "Could not git pull (offline or diverged); using local checkout as-is"
    fi
  else
    command -v git >/dev/null 2>&1 || die "git is required: https://git-scm.com/downloads"
    log "Cloning $REPO_URL -> $APP_DIR"
    clone_into_app_dir || die "git clone failed: $REPO_URL -> $APP_DIR (see error above)"
  fi
fi
START_DIR="$PWD"
# Make APP_DIR absolute BEFORE cd-ing into it: INSTALL_LIB below is built from
# $APP_DIR and is checked after the cd, so a relative APP_DIR (e.g. ./machina)
# would resolve to <checkout>/<checkout>/tools/... and falsely report "Missing".
case "$APP_DIR" in
  /*) : ;;
  *) APP_DIR="$PWD/${APP_DIR#./}" ;;
esac
cd "$APP_DIR" || die "Cannot cd to $APP_DIR"

# Testable installer primitives (function definitions only; sourcing has no
# side effects, so tests load this file directly). Needs APP_DIR first.
INSTALL_LIB="$APP_DIR/tools/install-helpers.sh"
[ -f "$INSTALL_LIB" ] || die "Missing $INSTALL_LIB (update the checkout)"
# shellcheck disable=SC1091
. "$INSTALL_LIB"

# The env-file target is resolved ONCE here: an absolute MACHINA_ENV_FILE wins
# as-is, a relative one anchors to the startup directory (a later directory
# can no longer redirect it), and unset means the checkout .env.
ENV_FILE="$(resolve_env_file "$START_DIR")"
# --- env-file parent guard ---------------------------------------------------
# MACHINA_ENV_FILE pointing into a missing or read-only directory used to abort
# below with a bare bash redirection error (`: > $ENV_FILE` / `cp .env.example`)
# and no explanation. Fail with installer guidance instead; the code between the
# markers is exercised verbatim by the guarded probe in the R4-095 proof doc.
ENV_PARENT="$(dirname "$ENV_FILE")"
if [ ! -d "$ENV_PARENT" ]; then
  die "MACHINA_ENV_FILE=$ENV_FILE cannot be used: directory $ENV_PARENT does not exist (create it, or point MACHINA_ENV_FILE at an existing directory)"
fi
if [ ! -w "$ENV_PARENT" ]; then
  die "MACHINA_ENV_FILE=$ENV_FILE cannot be written: directory $ENV_PARENT is not writable"
fi
# --- end env-file parent guard -----------------------------------------------
# Export the resolved absolute target: `machina init`/`doctor` below run as
# child processes from APP_DIR, where a relative MACHINA_ENV_FILE would
# otherwise resolve against the wrong directory (the startup dir differs).
export MACHINA_ENV_FILE="$ENV_FILE"
log "Using env file: $ENV_FILE"

# --- Prereqs -----------------------------------------------------------------
pick_python || die "Python 3.11+ is required: https://www.python.org/downloads/ (found: $(python3 --version 2>&1 || python --version 2>&1 || echo none))"
log "Python: $("$PYTHON_BIN" --version 2>&1)"

if [ "$DO_UI" = "1" ]; then
  if ! command -v node >/dev/null 2>&1; then
    warn "Node.js not found; UI will not work. Install 22.19+ from https://nodejs.org/ or re-run with --no-ui."
    DO_UI=0
  elif [ "$(node -e 'const p=process.versions.node.split(".").map(Number); console.log((p[0]>22||(p[0]===22&&p[1]>=19))?1:0)' 2>/dev/null || echo 0)" != "1" ]; then
    # F-08: ui/package.json engines and the launcher enforce 22.19+, so an
    # `npm ci` on an older Node can abort the whole install before .env, init,
    # or the PATH shim exist. Degrade the UI instead of hard-failing the backend.
    warn "Node $(node --version) found; 22.19+ is required for the TUI. Skipping UI dependencies (backend install continues; re-run with Node 22.19+ to build the UI)."
    DO_UI=0
  fi
  if [ "$DO_UI" = "1" ] && ! command -v npm >/dev/null 2>&1; then
    warn "npm not found; skipping UI dependencies (re-run without --no-ui once npm exists)."
    DO_UI=0
  fi
fi

# --- Python venv ---------------------------------------------------------------
# Never trust an existing .venv blindly: a stale/broken one (no interpreter
# inside) is the most common install failure. Verify, rebuild if needed, and
# always invoke the venv binaries by explicit path so a broken `activate`
# script (common on Git Bash/Windows) cannot break the install.
venv_python() {
  # Print the venv interpreter path, if present and executable.
  if [ -x ".venv/bin/python" ]; then
    printf '%s' ".venv/bin/python"
  elif [ -x ".venv/Scripts/python.exe" ]; then
    printf '%s' ".venv/Scripts/python.exe"
  else
    return 1
  fi
}

venv_healthy() {
  local py
  py="$(venv_python 2>/dev/null)" || return 1
  [ "$("$py" -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)' 2>/dev/null || echo 0)" = "1" ]
}

if venv_healthy; then
  log "Reusing healthy virtualenv (.venv)"
else
  if [ -d ".venv" ]; then
    warn "Existing .venv has no working python; rebuilding it"
    deactivate 2>/dev/null || true
  else
    log "Creating virtualenv (.venv) with $PYTHON_BIN"
  fi
  rm -rf .venv
  # F-32: guard the venv command itself; under `set -e` an unguarded failure
  # exits before the diagnostic below (Debian/Ubuntu without python3-venv).
  if ! "$PYTHON_BIN" -m venv .venv || ! venv_healthy; then
    die "Could not build a working .venv with $PYTHON_BIN. Try running '$PYTHON_BIN -m venv --clear .venv' manually (see error above), or install Python 3.11+ and the venv/ensurepip package for your distribution (e.g. sudo apt-get install python3-venv)."
  fi
  log "Virtualenv ready"
fi
VENV_PY="$(venv_python)"
VENV_BIN="$(dirname "$VENV_PY")"
MACHINA_BIN="$VENV_BIN/machina"

# Best-effort activation (convenience only; everything below uses $VENV_PY/$MACHINA_BIN).
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  . ".venv/bin/activate" 2>/dev/null || true
elif [ -f ".venv/Scripts/activate" ]; then
  # shellcheck disable=SC1091
  . ".venv/Scripts/activate" 2>/dev/null || true
fi

run_animated "Upgrading pip" "$VENV_PY" -m pip install --quiet --upgrade pip

if [ "$DO_DEV" = "1" ]; then
  run_animated "Installing Machina (dev extras)" "$VENV_PY" -m pip install -e ".[dev]"
else
  run_animated "Installing Machina" "$VENV_PY" -m pip install -e .
fi
# Deliberate F-15 exception: an editable checkout install follows the local
# pyproject so source edits take effect. CI and container builds use the
# authoritative `uv.lock` (uv export --frozen) instead; do not copy this
# resolver behavior into release automation.
# Re-resolve: the install creates the `machina` entry point (machina / machina.exe).
MACHINA_BIN="$VENV_BIN/machina"
[ -f "$MACHINA_BIN" ] || MACHINA_BIN="$VENV_BIN/machina.exe"
[ -f "$MACHINA_BIN" ] || die "Install succeeded but no 'machina' binary appeared in $VENV_BIN"

# --- UI deps -------------------------------------------------------------------
if [ "$DO_UI" = "1" ]; then
  run_animated "Installing UI dependencies" npm ci --ignore-scripts --prefix ui
else
  log "Skipping UI dependencies"
fi

# --- .env: defaults + generated secrets, prompt only when safe -----------------
# AH-007: generated API/provenance keys must not be world-readable. Use a
# restrictive umask for creation and enforce mode 0600 on the file itself.
# Every read/write below targets $ENV_FILE (MACHINA_ENV_FILE-aware); see
# resolve_env_file in tools/install-helpers.sh.
umask 077
# F-07: ANY missing env target (default or MACHINA_ENV_FILE override) is seeded
# from .env.example before gap-filling. A custom path is a location choice,
# not a statement that the file already exists.
if [ ! -f "$ENV_FILE" ]; then
  log "Creating $ENV_FILE from .env.example"
  cp .env.example "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || warn "could not restrict $ENV_FILE to mode 600; verify its permissions"
else
  log "$ENV_FILE already exists, keeping it (filling gaps only)"
  chmod 600 "$ENV_FILE" 2>/dev/null || warn "could not restrict $ENV_FILE to mode 600; verify its permissions"
fi

# Ensure DATABASE_URL exists.
DB_ENV_VALUE="$(env_get DATABASE_URL "$ENV_FILE")" || die "Cannot read $ENV_FILE"
if [ -z "$DB_ENV_VALUE" ]; then
  env_set DATABASE_URL "$INSTALLER_DEFAULT_DB_URL" "$ENV_FILE" \
    || die "Cannot write $ENV_FILE"
fi

# Replace placeholder secrets with real random values (mirrors `machina setup` autogenerate).
for KEY in MACHINA_API_KEY MACHINA_PROVENANCE_KEY; do
  VAL="$(env_get "$KEY" "$ENV_FILE")" || die "Cannot read $ENV_FILE"
  case "$VAL" in
    ""|"replace-with-a-long-random-key"|"replace-with-a-separate-long-random-key"|"sk-or-..."|"sk-...")
      NEW_VAL="$(rand_hex)"
      env_set "$KEY" "$NEW_VAL" "$ENV_FILE" || die "Cannot write $ENV_FILE"
      log "Generated random $KEY"
      ;;
  esac
done

# F-21: MCP OAuth needs a master key on headless/Docker hosts (no desktop
# keyring), so generate one when absent. Existing operator keys are preserved.
OAUTH_KEY="$(env_get MACHINA_OAUTH_MASTER_KEY "$ENV_FILE")" || die "Cannot read $ENV_FILE"
case "$OAUTH_KEY" in
  ""|"replace-with-a-base64-32-byte-key")
    OAUTH_KEY_NEW="$(rand_b64)"
    env_set MACHINA_OAUTH_MASTER_KEY "$OAUTH_KEY_NEW" "$ENV_FILE" || die "Cannot write $ENV_FILE"
    log "Generated MACHINA_OAUTH_MASTER_KEY (base64, 32 bytes)"
    ;;
esac

# Prompt for the LLM key only when we have a real TTY (or --interactive), never
# blocking a `curl | bash` run. Reads from /dev/tty so the pipe isn't consumed.
# AH-AUDIT-040b: typed secrets must not echo. prompt_secret_hidden fails
# unless hidden input is fully established — there is no visible-typing
# fallback; a failure skips with secure instructions instead.
if [ "$ASSUME_YES" = "0" ] && { [ -t 0 ] || [ "$INTERACTIVE" = "1" ]; } && [ -e /dev/tty ]; then
  CUR="$(env_get OPENROUTER_API_KEY "$ENV_FILE")" || die "Cannot read $ENV_FILE"
  case "$CUR" in
    ""|"sk-or-...")
      if ANSWER="$(prompt_secret_hidden 'OPENROUTER_API_KEY (Enter to skip, set later via machina setup): ')"; then
        if [ -n "$ANSWER" ]; then
          env_set OPENROUTER_API_KEY "$ANSWER" "$ENV_FILE" || die "Cannot write $ENV_FILE"
          log "Saved OPENROUTER_API_KEY"
        else
          warn "Skipped OPENROUTER_API_KEY; set it later with: machina setup"
        fi
      else
        warn "Could not establish hidden terminal input; skipping secure prompt."
        warn "Set the key later with: machina setup"
      fi
      ;;
  esac
else
  CUR="$(env_get OPENROUTER_API_KEY "$ENV_FILE")" || die "Cannot read $ENV_FILE"
  case "$CUR" in
    ""|"sk-or-...")
      warn "No OPENROUTER_API_KEY yet; install continues. Add one later with: machina setup"
      ;;
  esac
fi

# --- DB: try init, auto-start local pgvector via docker on failure -------------
# Provisioning primitives live in tools/install-helpers.sh (sourced above)
# so tests can drive them with mocked commands.

INIT_STATE="failed"
if [ "$SKIP_INIT" = "1" ]; then
  INIT_STATE="skipped"
  log "Skipping machina init (--skip-init)"
else
  log "Running machina init automatically"
  # DOCKER_DB_RC contract (INFRA-I1): start_docker_db returns 0 when a
  # container was started/reused safely, 1 when docker is unusable (Homebrew
  # may still help), and 2 when the existing machina-db container conflicts
  # with DATABASE_URL — auto-provisioning must stop there: retrying init for
  # 60 s and then installing Homebrew Postgres cannot repair a custom URL.
  DOCKER_DB_RC=1
  if INIT_OUTPUT="$(try_init 2>&1)"; then
    INIT_STATE="ready"
    log "Schema ready (machina init completed)"
  elif [ "$DB_AUTO" = "no" ]; then
    printf '%s\n' "$INIT_OUTPUT" >&2
    warn "\`machina init\` failed and auto-provisioning is off (--no-auto-db)."
    warn "Start Postgres (pgvector/pgvector:pg16), check DATABASE_URL in .env, then run: machina init"
  else
    if [ "$DOCKER_DB" != "no" ]; then
      DOCKER_DB_RC=0
      start_docker_db || DOCKER_DB_RC=$?
    fi
    if [ "$DOCKER_DB_RC" = "2" ]; then
      printf '%s\n' "$INIT_OUTPUT" >&2
      warn "\`machina init\` failed and the existing machina-db container conflicts with DATABASE_URL."
      warn "Stopping auto-provisioning: Homebrew cannot repair a custom DATABASE_URL, so no brew install is attempted."
    elif [ "$DOCKER_DB_RC" = "0" ] && INIT_OUTPUT="$(wait_for_init 2>&1)"; then
      INIT_STATE="ready"
      log "Schema ready (via auto-started docker Postgres)"
    elif [ "$DOCKER_DB" = "yes" ]; then
      # F-06: --with-docker-db is a hard constraint. Never install/start a
      # host Homebrew Postgres when the operator explicitly pinned Docker.
      printf '%s\n' "$INIT_OUTPUT" >&2
      warn "\`machina init\` failed and Docker Postgres was requested (--with-docker-db)."
      warn "Not falling back to Homebrew: start Docker (or fix machina-db/DATABASE_URL), then re-run: machina init"
    elif start_brew_db && INIT_OUTPUT="$(wait_for_init 2>&1)"; then
      INIT_STATE="ready"
      log "Schema ready (via Homebrew Postgres)"
    else
      printf '%s\n' "$INIT_OUTPUT" >&2
      warn "\`machina init\` failed. Pick one, then re-run: machina init"
      case "$OS_FAMILY" in
        macos)
          warn "  docker:  docker run -d --name machina-db -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=<generated> -e POSTGRES_DB=machina -p 127.0.0.1:5432:5432 pgvector/pgvector:pg16"
          warn "           (then set DATABASE_URL=postgresql://postgres:<generated>@127.0.0.1:5432/machina in .env, mode 0600)"
          warn "  brew:    brew install postgresql@16 pgvector && brew services start postgresql@16"
          warn "           createuser -s machina && createdb -O machina machina && psql -U machina -d machina -c 'CREATE EXTENSION vector;'"
          ;;
        windows)
          warn "  docker (Docker Desktop): same docker run line as above"
          warn "  native:  winget install -e --id PostgreSQL.16 (then add pgvector via StackBuilder), create the machina DB, set DATABASE_URL in .env"
          ;;
        *)
          warn "  docker:  docker run -d --name machina-db -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=<generated> -e POSTGRES_DB=machina -p 127.0.0.1:5432:5432 pgvector/pgvector:pg16"
          warn "  apt:     sudo apt-get install -y postgresql postgresql-contrib (plus pgvector for your PG major), create the machina DB, set DATABASE_URL in .env"
          ;;
      esac
    fi
  fi
fi

log "Checking setup (machina doctor; DB/key warnings are OK, see hints above)"
"$MACHINA_BIN" doctor || true

# --- PATH persistence ----------------------------------------------------------
# A piped `curl | bash` runs in a subshell, so `export PATH=...` would die with
# it. Instead we persist ~/.local/bin into the shell rc file (idempotent) and,
# on Windows, register the venv Scripts dir in the user Path via PowerShell so
# `machina` also works in PowerShell/cmd — all without prompting.
# --- Path quoting for generated code -------------------------------------------
# R5-9: the shim, the shell-rc line, and the Windows Path command interpolate
# absolute paths into executable code. A valid install path containing `$`,
# backticks, quotes, or backslashes was reinterpreted (or executed) when that
# code ran. Escape paths for the exact quoting layer they land in:
#   sh_dquote_escape: `\, ", $, ` escaped for a double-quoted POSIX shell word.
#   fish_quote: single-quote a word for fish (escape `\` and `'`).
#   ps_squote_escape: `'` doubled for a single-quoted PowerShell string
#     (where `$`, `"`, and `` ` `` are already literal).
# Plain paths pass through every helper unchanged, so normal installs are
# byte-identical to before.
sh_dquote_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\$/\\$/g' -e 's/`/\\`/g'
}
fish_quote() {
  # Safe paths stay bare (byte-identical to before, so the rc idempotency
  # check keeps matching across upgrades); anything else is single-quoted.
  # Escape first, wrap second (the wrapping quotes stay bare).
  case "$1" in
    *[[:space:]]*|*\'*|*\"*|*\`*|*\$*|*\\*|*\**|*\?*|*\#*|*\(*|*\)*|*\{*|*\}*|*\[*|*\]*|*\;*|*\|*|*\&*|*\<*|*\>*) ;;
    *) printf '%s' "$1"; return 0 ;;
  esac
  local escaped
  escaped="$(printf '%s' "$1" | sed -e 's/\\/\\\\/g')"
  escaped="$(printf '%s' "$escaped" | sed "s/'/\\\\'/g")"
  printf "'%s'" "$escaped"
}
ps_squote_escape() {
  printf '%s' "$1" | sed "s/'/''/g"
}

shell_rc_file() {
  case "${SHELL:-}" in
    *fish*) printf '%s' "$HOME/.config/fish/config.fish" ;;
    *zsh*) printf '%s' "$HOME/.zshrc" ;;
    *bash*)
      if [ "$OS_FAMILY" = "macos" ]; then printf '%s' "$HOME/.bash_profile"
      else printf '%s' "$HOME/.bashrc"; fi
      ;;
    *)
      if [ "$OS_FAMILY" = "macos" ]; then printf '%s' "$HOME/.zshrc"
      else printf '%s' "$HOME/.profile"; fi
      ;;
  esac
}

PATH_STATE="unknown" # on-path | added:<rc> | already:<rc> | hint-only | shim-failed
PATH_RC=""
ensure_path_in_rc() {
  local dir="$1" rc line
  if [ "$SHELL_RC" = "no" ]; then
    warn "Add to PATH once (then restart your shell):"
    warn "  zsh (macOS default): echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
    warn "  bash:                echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc"
    warn "  current shell only:  export PATH=\"\$HOME/.local/bin:\$PATH\""
    PATH_STATE="hint-only"
    return 0
  fi
  case "${SHELL:-}" in
    *fish*) line="set -gx PATH $(fish_quote "$dir") \$PATH" ;;
    *) line="export PATH=\"$(sh_dquote_escape "$dir"):\$PATH\"" ;;
  esac
  rc="$(shell_rc_file)"
  PATH_RC="$rc"
  mkdir -p "$(dirname "$rc")" 2>/dev/null || true
  touch "$rc" 2>/dev/null || {
    warn "Cannot write $rc; add this line yourself: $line"
    PATH_STATE="hint-only"
    return 0
  }
  if grep -Fqx "$line" "$rc" 2>/dev/null; then
    log "$dir already on PATH in $rc"
    PATH_STATE="already:$rc"
  else
    printf '\n# Machina installer: `machina` on PATH\n%s\n' "$line" >> "$rc"
    log "Added $dir to PATH in $rc — restart your shell (or: source $rc)"
    PATH_STATE="added:$rc"
  fi
}

register_windows_path() {
  # On Windows, also register the venv Scripts dir in the *user* Path so `machina`
  # resolves in PowerShell/cmd, not just Git Bash. Idempotent, best-effort.
  [ "$OS_FAMILY" = "windows" ] || return 0
  command -v powershell.exe >/dev/null 2>&1 || return 0
  command -v cygpath >/dev/null 2>&1 || return 0
  local windir windir_ps
  windir="$(cygpath -w "$(pwd)/$VENV_BIN" 2>/dev/null || true)"
  [ -n "$windir" ] || return 0
  # R5-9: the path lands inside single-quoted PowerShell strings, where a
  # raw `'` (e.g. an O'Connor profile dir) would terminate the string. Double
  # it; every other character is already literal in PS single quotes, and the
  # bash "$..." expansion inserts the value literally, so plain paths are
  # unchanged.
  windir_ps="$(ps_squote_escape "$windir")"
  if powershell.exe -NoProfile -NonInteractive -Command \
    "[Environment]::SetEnvironmentVariable('Path', (([Environment]::GetEnvironmentVariable('Path','User') -split ';' | Where-Object { \$_ -ne '$windir_ps' }) + '$windir_ps') -join ';', 'User')" \
    >/dev/null 2>&1; then
    log "Registered $windir in Windows user Path (new PowerShell windows will see \`machina\`)"
  else
    warn "Could not update Windows user Path; in PowerShell run:"
    warn "  [Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path','User') + ';$windir_ps', 'User')"
  fi
}

# --- PATH shim so `machina` just works ------------------------------------------------
# Absolute path: APP_DIR may be relative (e.g. ./machina), which would
# bake a broken relative exec into the shim.
VENV_MACHINA="$(pwd)/$MACHINA_BIN"
SHIM_DIR="$HOME/.local/bin"

# F-05: `command -v machina` on Git Bash/MSYS may print the Scripts path
# without the `.exe` suffix that VENV_MACHINA carries, and a previous install
# can leave our own shim pointing at a moved/deleted checkout. Normalize both
# sides (`.exe`, Windows drive form, case) and treat "$SHIM_DIR/machina" as
# ours to replace — it is never a foreign launcher worth preserving.
normalize_bin_path() {
  local p="$1"
  p="${p%.exe}"
  case "$OS_FAMILY" in
    windows)
      if command -v cygpath >/dev/null 2>&1; then
        p="$(cygpath -u "$p" 2>/dev/null || printf '%s' "$p")"
      fi
      p="$(printf '%s' "$p" | tr '[:upper:]' '[:lower:]')"
      ;;
  esac
  printf '%s' "$p"
}

if [ -f "$VENV_MACHINA" ]; then
  # NOTE: don't trust `command -v machina` alone here — during install the venv is
  # activated, so `machina` resolves even when no other shell can see it. Only skip
  # the shim when `machina` resolves to a different, foreign launcher.
  existing_machina="$(command -v machina 2>/dev/null || true)"
  if [ -n "$existing_machina" ] \
    && [ "$(normalize_bin_path "$existing_machina")" != "$(normalize_bin_path "$VENV_MACHINA")" ] \
    && [ "$existing_machina" != "$SHIM_DIR/machina" ]; then
    log "\`machina\` already on PATH: $existing_machina (leaving it)"
  elif mkdir -p "$SHIM_DIR" 2>/dev/null; then
    if [ "$existing_machina" = "$SHIM_DIR/machina" ]; then
      log "Replacing stale \`machina\` shim at $SHIM_DIR/machina"
    fi
    # R5-9: escape the baked-in path for the double-quoted sh word it lands
    # in — a raw `$`, backtick, `"`, or `\` would be reinterpreted (or
    # executed) every time the shim runs. Plain paths are unchanged.
    SHIM_TARGET="$(sh_dquote_escape "$VENV_MACHINA")"
    cat > "$SHIM_DIR/machina" <<EOF2
#!/usr/bin/env sh
exec "$SHIM_TARGET" "\$@"
EOF2
    chmod +x "$SHIM_DIR/machina"
    log "Installed \`machina\` shim to $SHIM_DIR/machina"
    case ":$PATH:" in
      *":$SHIM_DIR:"*)
        log "$SHIM_DIR already on PATH in this shell"
        PATH_STATE="on-path"
        ;;
      *) ensure_path_in_rc "$SHIM_DIR" ;;
    esac
  else
    warn "Could not write $SHIM_DIR/machina; activate the venv instead: source .venv/bin/activate"
    PATH_STATE="shim-failed"
  fi
else
  PATH_STATE="shim-failed"
fi
register_windows_path

# A piped `curl | bash` cannot export PATH into the caller's shell, and the
# rc-file note scrolls by mid-install — so repeat the exact fix here, in the
# final block the user actually reads. This is the macOS `command not found`
# case: ~/.zshrc was updated but this terminal hasn't reloaded it yet.
case ":$PATH:" in
  *":$SHIM_DIR:"*) PATH_NOW=1 ;;
  *) PATH_NOW=0 ;;
esac
if [ "$PATH_NOW" = "0" ] && [ "$PATH_STATE" != "shim-failed" ] && [ -f "$SHIM_DIR/machina" ]; then
  cat <<EOF

[action needed] \`machina\` is installed but not on PATH in THIS shell yet.
Run this one line now (new terminals pick it up automatically):
  export PATH="\$HOME/.local/bin:\$PATH"
EOF
  case "$PATH_STATE" in
    added:*|already:*) printf 'Persisted in %s — or just restart your terminal.\n' "${PATH_STATE#*:}" ;;
  esac
  echo ""
fi

if [ "$PATH_STATE" = "shim-failed" ]; then
  warn "No \`machina\` shim was installed; use the venv directly:"
  warn "  source \"$(pwd)/.venv/bin/activate\" && machina"
  echo ""
fi

# --- Interop: register Machina's MCP server with detected harnesses -----------
# Runs after the shim step (so the `machina` CLI exists) and is failure-tolerant
# like the init step: an interop problem must never fail the install. `none`
# skips entirely; `all` registers every detected harness; `auto` prompts only
# on an interactive TTY and otherwise just prints the detection summary plus
# the exact enable-later commands (`--yes`/piped runs never prompt).
INTEROP_PROMPT="prompt"
# F-03: a piped `curl | bash --interactive` has no TTY on stdin but can still
# prompt through /dev/tty. Only suppress prompting when the user asked for
# non-interactive mode or no terminal exists at all.
if [ "$ASSUME_YES" = "1" ] || { [ ! -t 0 ] && [ "$INTERACTIVE" != "1" ]; }; then
  INTEROP_PROMPT="no-prompt"
elif [ ! -e /dev/tty ]; then
  INTEROP_PROMPT="no-prompt"
fi
installer_interop_setup "$INTEROP_MODE" "$INTEROP_PROMPT"

installer_next_steps "$INIT_STATE" "$(pwd)/README.md"
