#!/usr/bin/env bash
# AgentHarness zero-friction installer.
#
# Just works:
#   curl -fsSL https://raw.githubusercontent.com/kiazh/Agent-Harness/main/install.sh | bash
#
# With options (note the `--` after `-s` when piping via curl):
#   curl -fsSL https://raw.githubusercontent.com/kiazh/Agent-Harness/main/install.sh | bash -s -- --dir ~/agent-harness --dev
#   curl -fsSL https://raw.githubusercontent.com/kiazh/Agent-Harness/main/install.sh | bash -s -- --interactive
#
# Local:
#   ./install.sh [--dir DIR] [--dev] [--no-ui] [--interactive] [--yes] [--with-docker-db|--no-docker-db] [--skip-init]
#
# What it does, in order:
#   1. Uses current checkout (or clones REPO_URL into --dir / ./agent-harness)
#   2. Checks python>=3.11, node>=22.19 (UI only), npm, git
#   3. Creates .venv, pip install -e . (or .[dev])
#   4. npm install --prefix ui (unless --no-ui)
#   5. Creates .env with sane defaults + random API/provenance keys (never prompts when piped)
#   6. Tries `ah init`; if Postgres is down and docker exists, starts pgvector/pgvector:pg16
#      on localhost:5432 automatically and retries
#   7. Installs an `ah` shim on PATH (~/.local/bin) so `ah` just works after restart
#   8. Prints exact next steps. Missing LLM key is a warning, never a fatal error.
#
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/kiazh/Agent-Harness.git}"
INSTALL_DIR="${AH_INSTALL_DIR:-}"
DO_DEV=0
DO_UI=1
INTERACTIVE=0
ASSUME_YES=0
SKIP_INIT=0
DOCKER_DB="auto" # auto | yes | no
PYTHON_BIN=""

log()  { printf '\033[1;32m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  --dir DIR        Install directory (default: ./agent-harness when cloning,
                   otherwise current directory if already a checkout)
  --dev            Install Python dev extras (pip install -e ".[dev]")
  --no-ui          Skip UI dependencies (npm install --prefix ui)
  --interactive    Prompt for OPENROUTER_API_KEY even when piped via curl
  -y, --yes, --non-interactive
                   Never prompt; generate defaults and finish (default when piped)
  --with-docker-db Force auto-start of local Postgres via docker on init failure
  --no-docker-db   Never touch docker; just warn on init failure
  --skip-init      Skip `ah init` (DB schema creation)
  -h, --help       Show this help

Env: REPO_URL, AH_INSTALL_DIR
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) INSTALL_DIR="${2:-}"; shift 2 ;;
    --dev) DO_DEV=1; shift ;;
    --no-ui) DO_UI=0; shift ;;
    --interactive) INTERACTIVE=1; shift ;;
    -y|--yes|--non-interactive) ASSUME_YES=1; shift ;;
    --with-docker-db) DOCKER_DB="yes"; shift ;;
    --no-docker-db) DOCKER_DB="no"; shift ;;
    --skip-init) SKIP_INIT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
done

# Piped via `curl | bash` => stdin is not a TTY => default to non-interactive.
if [ ! -t 0 ] && [ "$INTERACTIVE" = "0" ]; then
  ASSUME_YES=1
fi

is_repo_dir() { [ -f "$1/pyproject.toml" ] && [ -d "$1/ah" ] && [ -d "$1/ui" ]; }

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

# --- Locate or clone the checkout -------------------------------------------
if is_repo_dir "$(pwd)"; then
  APP_DIR="$(pwd)"
  log "Using current checkout: $APP_DIR"
else
  if [ -z "$INSTALL_DIR" ]; then INSTALL_DIR="./agent-harness"; fi
  APP_DIR="$INSTALL_DIR"
  if [ -d "$APP_DIR" ] && is_repo_dir "$APP_DIR"; then
    log "Using existing checkout: $APP_DIR"
  else
    command -v git >/dev/null 2>&1 || die "git is required: https://git-scm.com/downloads"
    log "Cloning $REPO_URL -> $APP_DIR"
    git clone --depth 1 "$REPO_URL" "$APP_DIR"
  fi
fi
cd "$APP_DIR" || die "Cannot cd to $APP_DIR"

# --- Prereqs -----------------------------------------------------------------
pick_python || die "Python 3.11+ is required: https://www.python.org/downloads/ (found: $(python3 --version 2>&1 || python --version 2>&1 || echo none))"
log "Python: $("$PYTHON_BIN" --version 2>&1)"

if [ "$DO_UI" = "1" ]; then
  if ! command -v node >/dev/null 2>&1; then
    warn "Node.js not found; UI will not work. Install 22.19+ from https://nodejs.org/ or re-run with --no-ui."
    DO_UI=0
  elif [ "$(node -e 'const p=process.versions.node.split(".").map(Number); console.log((p[0]>22||(p[0]===22&&p[1]>=19))?1:0)' 2>/dev/null || echo 0)" != "1" ]; then
    warn "Node $(node --version) found; 22.19+ recommended. Continuing anyway."
  fi
  if [ "$DO_UI" = "1" ] && ! command -v npm >/dev/null 2>&1; then
    warn "npm not found; skipping UI dependencies (re-run without --no-ui once npm exists)."
    DO_UI=0
  fi
fi

# --- Python venv ---------------------------------------------------------------
if [ ! -d ".venv" ]; then
  log "Creating virtualenv (.venv)"
  "$PYTHON_BIN" -m venv .venv
fi

if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  . ".venv/bin/activate"
elif [ -f ".venv/Scripts/activate" ]; then
  # shellcheck disable=SC1091
  . ".venv/Scripts/activate"
else
  die "Virtualenv activation script not found in .venv"
fi
PYTHON_BIN="python" # inside the venv, `python` is the right interpreter

log "Upgrading pip"
python -m pip install --quiet --upgrade pip

if [ "$DO_DEV" = "1" ]; then
  log "Installing AgentHarness (dev extras)"
  pip install -e ".[dev]"
else
  log "Installing AgentHarness"
  pip install -e .
fi

# --- UI deps -------------------------------------------------------------------
if [ "$DO_UI" = "1" ]; then
  log "Installing UI dependencies (npm --prefix ui)"
  npm install --ignore-scripts --prefix ui
else
  log "Skipping UI dependencies"
fi

# --- .env: defaults + generated secrets, prompt only when safe -----------------
if [ ! -f ".env" ]; then
  log "Creating .env from .env.example"
  cp .env.example .env
else
  log ".env already exists, keeping it (filling gaps only)"
fi

# Ensure DATABASE_URL exists.
if ! grep -q '^DATABASE_URL=' .env; then
  printf '\nDATABASE_URL=postgresql://postgres:postgres@localhost:5432/agentharness\n' >> .env
fi

# Replace placeholder secrets with real random values (mirrors `ah setup` autogenerate).
for KEY in AGENT_HARNESS_API_KEY AGENT_HARNESS_PROVENANCE_KEY; do
  VAL="$(grep -E "^${KEY}=" .env | cut -d= -f2- || true)"
  case "$VAL" in
    ""|"replace-with-a-long-random-key"|"replace-with-a-separate-long-random-key"|"sk-or-..."|"sk-...")
      NEW_VAL="$(rand_hex)"
      # Portable in-place replace: works on GNU + BSD sed via temp file.
      grep -v -E "^${KEY}=" .env > .env.tmp || true
      printf '%s=%s\n' "$KEY" "$NEW_VAL" >> .env.tmp
      mv .env.tmp .env
      log "Generated random $KEY"
      ;;
  esac
done

# Prompt for the LLM key only when we have a real TTY (or --interactive), never
# blocking a `curl | bash` run. Reads from /dev/tty so the pipe isn't consumed.
if [ "$ASSUME_YES" = "0" ] && { [ -t 0 ] || [ "$INTERACTIVE" = "1" ]; } && [ -e /dev/tty ]; then
  CUR="$(grep -E '^OPENROUTER_API_KEY=' .env | cut -d= -f2- || true)"
  case "$CUR" in
    ""|"sk-or-...")
      printf 'OPENROUTER_API_KEY (Enter to skip, set later via /keys): ' > /dev/tty
      read -r ANSWER < /dev/tty || ANSWER=""
      if [ -n "$ANSWER" ]; then
        grep -v -E '^OPENROUTER_API_KEY=' .env > .env.tmp || true
        printf 'OPENROUTER_API_KEY=%s\n' "$ANSWER" >> .env.tmp
        mv .env.tmp .env
        log "Saved OPENROUTER_API_KEY"
      else
        warn "Skipped OPENROUTER_API_KEY; set it later with: ah setup  (or /keys in the UI)"
      fi
      ;;
  esac
else
  case "$(grep -E '^OPENROUTER_API_KEY=' .env | cut -d= -f2- || true)" in
    ""|"sk-or-...")
      warn "No OPENROUTER_API_KEY yet; install continues. Add one later with: ah setup  (or /keys in the UI)"
      ;;
  esac
fi

# --- DB: try init, auto-start local pgvector via docker on failure -------------
try_init() { ah init 2>&1; }

start_docker_db() {
  command -v docker >/dev/null 2>&1 || return 1
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^agentharness-db$'; then
    log "Starting existing agentharness-db container"
    docker start agentharness-db >/dev/null
    return 0
  fi
  log "Starting local Postgres (pgvector/pgvector:pg16 on localhost:5432)"
  # shellcheck disable=SC2086
  docker run -d --name agentharness-db \
    -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=agentharness \
    -p 5432:5432 pgvector/pgvector:pg16 >/dev/null || return 1
  return 0
}

wait_for_init() {
  local i
  for i in $(seq 1 30); do
    if try_init >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

if [ "$SKIP_INIT" = "1" ]; then
  log "Skipping ah init (--skip-init)"
else
  log "Initializing database schema (ah init)"
  if try_init; then
    log "Schema ready"
  elif [ "$DOCKER_DB" = "no" ]; then
    warn "`ah init` failed. Start Postgres (pgvector/pgvector:pg16), check DATABASE_URL in .env, then run: ah init"
  elif start_docker_db && wait_for_init; then
    log "Schema ready (via auto-started docker Postgres)"
  else
    warn "`ah init` failed. Fixes:"
    warn "  1) docker path (recommended): docker start agentharness-db 2>/dev/null || docker run -d --name agentharness-db -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=agentharness -p 5432:5432 pgvector/pgvector:pg16"
    warn "  2) then: ah init"
    warn "  3) or point DATABASE_URL in .env at your Postgres and re-run ah init"
  fi
fi

log "Checking setup (ah doctor; DB/key warnings are OK, see hints above)"
ah doctor || true

# --- PATH shim so `ah` just works ------------------------------------------------
VENV_AH="$APP_DIR/.venv/bin/ah"
[ -f "$VENV_AH" ] || VENV_AH="$APP_DIR/.venv/Scripts/ah"
SHIM_DIR="$HOME/.local/bin"
if [ -f "$VENV_AH" ]; then
  if command -v ah >/dev/null 2>&1; then
    log "\`ah\` already on PATH: $(command -v ah)"
  elif mkdir -p "$SHIM_DIR" 2>/dev/null; then
    cat > "$SHIM_DIR/ah" <<EOF2
#!/usr/bin/env sh
exec "$VENV_AH" "\$@"
EOF2
    chmod +x "$SHIM_DIR/ah"
    log "Installed \`ah\` shim to $SHIM_DIR/ah"
    case ":$PATH:" in
      *":$SHIM_DIR:"*) ;;
      *) warn "Add to PATH once: export PATH=\"\$HOME/.local/bin:\$PATH\"  (then restart your shell)" ;;
    esac
  else
    warn "Could not write $SHIM_DIR/ah; activate the venv instead: source .venv/bin/activate"
  fi
fi

cat <<EOF

Done. Next:
  ah                          # interactive terminal UI
  ah chat "What is the capital of France?"   # one-shot (needs OPENROUTER_API_KEY)
  ah serve --host 127.0.0.1 --port 8000      # HTTP API

Keys:   ah setup  (or /keys in the UI; values never echo)
Health: ah doctor
Docs:   $APP_DIR/README.md
EOF
