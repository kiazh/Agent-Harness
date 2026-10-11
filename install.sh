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
#   6. Tries `ah init`; on failure auto-provisions Postgres (docker, then
#      Homebrew on macOS) and retries. OS-aware: macos | windows | linux.
#   7. Installs an `ah` shim in ~/.local/bin, persists it to the shell rc
#      (~/.zshrc / ~/.bash_profile / ~/.bashrc), and on Windows registers the
#      venv in the user Path for PowerShell — so `ah` works in new shells.
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
DB_AUTO="yes" # provision a local Postgres (docker, then Homebrew on macOS) when ah init fails
SHELL_RC="yes" # append ~/.local/bin to PATH in the shell rc file (opt out with --no-shell-rc)
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
  --no-docker-db   Never use docker for the DB
  --no-auto-db     Never auto-provision Postgres (docker or Homebrew); just warn
  --no-shell-rc    Don't touch shell rc files (~/.zshrc etc.); print PATH hint only
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
    --no-auto-db) DB_AUTO="no"; shift ;;
    --no-shell-rc) SHELL_RC="no"; shift ;;
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
if is_repo_dir "$(pwd)"; then
  APP_DIR="$(pwd)"
  log "Using current checkout: $APP_DIR"
else
  if [ -z "$INSTALL_DIR" ]; then INSTALL_DIR="./agent-harness"; fi
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
    git clone --depth 1 "$REPO_URL" "$APP_DIR"
  fi
fi
START_DIR="$PWD"
cd "$APP_DIR" || die "Cannot cd to $APP_DIR"

# Testable installer primitives (function definitions only; sourcing has no
# side effects, so tests load this file directly). Needs APP_DIR first.
INSTALL_LIB="$APP_DIR/tools/install-helpers.sh"
[ -f "$INSTALL_LIB" ] || die "Missing $INSTALL_LIB (update the checkout)"
# shellcheck disable=SC1091
. "$INSTALL_LIB"

# The env-file target is resolved ONCE here: an absolute AH_ENV_FILE wins
# as-is, a relative one anchors to the startup directory (a later directory
# can no longer redirect it), and unset means the checkout .env.
if [ -n "${AH_ENV_FILE:-}" ]; then ENV_IS_DEFAULT=0; else ENV_IS_DEFAULT=1; fi
ENV_FILE="$(resolve_env_file "$START_DIR")"
log "Using env file: $ENV_FILE"

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
  "$PYTHON_BIN" -m venv .venv
  venv_healthy || die "Could not build a working .venv with $PYTHON_BIN. Try running '$PYTHON_BIN -m venv --clear .venv' manually (see error above), or install Python 3.11+ from https://www.python.org/downloads/"
  log "Virtualenv ready"
fi
VENV_PY="$(venv_python)"
VENV_BIN="$(dirname "$VENV_PY")"
AH_BIN="$VENV_BIN/ah"

# Best-effort activation (convenience only; everything below uses $VENV_PY/$AH_BIN).
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  . ".venv/bin/activate" 2>/dev/null || true
elif [ -f ".venv/Scripts/activate" ]; then
  # shellcheck disable=SC1091
  . ".venv/Scripts/activate" 2>/dev/null || true
fi

log "Upgrading pip ($VENV_PY)"
"$VENV_PY" -m pip install --quiet --upgrade pip

if [ "$DO_DEV" = "1" ]; then
  log "Installing AgentHarness (dev extras)"
  "$VENV_PY" -m pip install -e ".[dev]"
else
  log "Installing AgentHarness"
  "$VENV_PY" -m pip install -e .
fi
# Re-resolve: the install creates the `ah` entry point (ah / ah.exe).
AH_BIN="$VENV_BIN/ah"
[ -f "$AH_BIN" ] || AH_BIN="$VENV_BIN/ah.exe"
[ -f "$AH_BIN" ] || die "Install succeeded but no 'ah' binary appeared in $VENV_BIN"

# --- UI deps -------------------------------------------------------------------
if [ "$DO_UI" = "1" ]; then
  log "Installing UI dependencies (npm --prefix ui)"
  npm install --ignore-scripts --prefix ui
else
  log "Skipping UI dependencies"
fi

# --- .env: defaults + generated secrets, prompt only when safe -----------------
# AH-007: generated API/provenance keys must not be world-readable. Use a
# restrictive umask for creation and enforce mode 0600 on the file itself.
# Every read/write below targets $ENV_FILE (AH_ENV_FILE-aware); see
# resolve_env_file in tools/install-helpers.sh.
umask 077
if [ "$ENV_IS_DEFAULT" = "1" ] && [ ! -f "$ENV_FILE" ]; then
  log "Creating .env from .env.example"
  cp .env.example "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
else
  log "$ENV_FILE already exists (or is overridden), keeping it (filling gaps only)"
  [ -f "$ENV_FILE" ] || : > "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
fi

# Ensure DATABASE_URL exists.
if [ -z "$(env_get DATABASE_URL "$ENV_FILE")" ]; then
  env_set DATABASE_URL "$INSTALLER_DEFAULT_DB_URL" "$ENV_FILE" \
    || die "Cannot write $ENV_FILE"
fi

# Replace placeholder secrets with real random values (mirrors `ah setup` autogenerate).
for KEY in AGENT_HARNESS_API_KEY AGENT_HARNESS_PROVENANCE_KEY; do
  VAL="$(env_get "$KEY" "$ENV_FILE")"
  case "$VAL" in
    ""|"replace-with-a-long-random-key"|"replace-with-a-separate-long-random-key"|"sk-or-..."|"sk-...")
      NEW_VAL="$(rand_hex)"
      env_set "$KEY" "$NEW_VAL" "$ENV_FILE" || die "Cannot write $ENV_FILE"
      log "Generated random $KEY"
      ;;
  esac
done

# Prompt for the LLM key only when we have a real TTY (or --interactive), never
# blocking a `curl | bash` run. Reads from /dev/tty so the pipe isn't consumed.
# AH-AUDIT-040b: typed secrets must not echo. prompt_secret_hidden fails
# unless hidden input is fully established — there is no visible-typing
# fallback; a failure skips with secure instructions instead.
if [ "$ASSUME_YES" = "0" ] && { [ -t 0 ] || [ "$INTERACTIVE" = "1" ]; } && [ -e /dev/tty ]; then
  CUR="$(env_get OPENROUTER_API_KEY "$ENV_FILE")"
  case "$CUR" in
    ""|"sk-or-...")
      if ANSWER="$(prompt_secret_hidden 'OPENROUTER_API_KEY (Enter to skip, set later via /keys): ')"; then
        if [ -n "$ANSWER" ]; then
          env_set OPENROUTER_API_KEY "$ANSWER" "$ENV_FILE" || die "Cannot write $ENV_FILE"
          log "Saved OPENROUTER_API_KEY"
        else
          warn "Skipped OPENROUTER_API_KEY; set it later with: ah setup  (or /keys in the UI)"
        fi
      else
        warn "Could not establish hidden terminal input; skipping secure prompt."
        warn "Set the key later with: ah setup  (or /keys in the UI)"
      fi
      ;;
  esac
else
  case "$(env_get OPENROUTER_API_KEY "$ENV_FILE")" in
    ""|"sk-or-...")
      warn "No OPENROUTER_API_KEY yet; install continues. Add one later with: ah setup  (or /keys in the UI)"
      ;;
  esac
fi

# --- DB: try init, auto-start local pgvector via docker on failure -------------
# Provisioning primitives live in tools/install-helpers.sh (sourced above)
# so tests can drive them with mocked commands.

if [ "$SKIP_INIT" = "1" ]; then
  log "Skipping ah init (--skip-init)"
else
  log "Initializing database schema (ah init)"
  if try_init; then
    log "Schema ready"
  elif [ "$DB_AUTO" = "no" ]; then
    warn "\`ah init\` failed and auto-provisioning is off (--no-auto-db)."
    warn "Start Postgres (pgvector/pgvector:pg16), check DATABASE_URL in .env, then run: ah init"
  elif [ "$DOCKER_DB" != "no" ] && start_docker_db && wait_for_init; then
    log "Schema ready (via auto-started docker Postgres)"
  elif start_brew_db && wait_for_init; then
    log "Schema ready (via Homebrew Postgres)"
  else
    warn "\`ah init\` failed. Pick one, then re-run: ah init"
    case "$OS_FAMILY" in
      macos)
        warn "  docker:  docker run -d --name agentharness-db -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=<generated> -e POSTGRES_DB=agentharness -p 127.0.0.1:5432:5432 pgvector/pgvector:pg16"
        warn "           (then set DATABASE_URL=postgresql://postgres:<generated>@127.0.0.1:5432/agentharness in .env, mode 0600)"
        warn "  brew:    brew install postgresql@16 pgvector && brew services start postgresql@16"
        warn "           createuser -s agentharness && createdb -O agentharness agentharness && psql -U agentharness -d agentharness -c 'CREATE EXTENSION vector;'"
        ;;
      windows)
        warn "  docker (Docker Desktop): same docker run line as above"
        warn "  native:  winget install -e --id PostgreSQL.16 (then add pgvector via StackBuilder), create the agentharness DB, set DATABASE_URL in .env"
        ;;
      *)
        warn "  docker:  docker run -d --name agentharness-db -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=<generated> -e POSTGRES_DB=agentharness -p 127.0.0.1:5432:5432 pgvector/pgvector:pg16"
        warn "  apt:     sudo apt-get install -y postgresql postgresql-contrib (plus pgvector for your PG major), create the agentharness DB, set DATABASE_URL in .env"
        ;;
    esac
  fi
fi

log "Checking setup (ah doctor; DB/key warnings are OK, see hints above)"
"$AH_BIN" doctor || true

# --- PATH persistence ----------------------------------------------------------
# A piped `curl | bash` runs in a subshell, so `export PATH=...` would die with
# it. Instead we persist ~/.local/bin into the shell rc file (idempotent) and,
# on Windows, register the venv Scripts dir in the user Path via PowerShell so
# `ah` also works in PowerShell/cmd — all without prompting.
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
    *fish*) line="set -gx PATH $dir \$PATH" ;;
    *) line="export PATH=\"$dir:\$PATH\"" ;;
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
    printf '\n# AgentHarness installer: `ah` on PATH\n%s\n' "$line" >> "$rc"
    log "Added $dir to PATH in $rc — restart your shell (or: source $rc)"
    PATH_STATE="added:$rc"
  fi
}

register_windows_path() {
  # On Windows, also register the venv Scripts dir in the *user* Path so `ah`
  # resolves in PowerShell/cmd, not just Git Bash. Idempotent, best-effort.
  [ "$OS_FAMILY" = "windows" ] || return 0
  command -v powershell.exe >/dev/null 2>&1 || return 0
  command -v cygpath >/dev/null 2>&1 || return 0
  local windir
  windir="$(cygpath -w "$(pwd)/$VENV_BIN" 2>/dev/null || true)"
  [ -n "$windir" ] || return 0
  if powershell.exe -NoProfile -NonInteractive -Command \
    "[Environment]::SetEnvironmentVariable('Path', (([Environment]::GetEnvironmentVariable('Path','User') -split ';' | Where-Object { \$_ -ne '$windir' }) + '$windir') -join ';', 'User')" \
    >/dev/null 2>&1; then
    log "Registered $windir in Windows user Path (new PowerShell windows will see \`ah\`)"
  else
    warn "Could not update Windows user Path; in PowerShell run:"
    warn "  [Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path','User') + ';$windir', 'User')"
  fi
}

# --- PATH shim so `ah` just works ------------------------------------------------
# Absolute path: APP_DIR may be relative (e.g. ./agent-harness), which would
# bake a broken relative exec into the shim.
VENV_AH="$(pwd)/$AH_BIN"
SHIM_DIR="$HOME/.local/bin"
if [ -f "$VENV_AH" ]; then
  # NOTE: don't trust `command -v ah` alone here — during install the venv is
  # activated, so `ah` resolves even when no other shell can see it. Only skip
  # the shim when `ah` resolves to something outside this venv.
  existing_ah="$(command -v ah 2>/dev/null || true)"
  if [ -n "$existing_ah" ] && [ "$existing_ah" != "$VENV_AH" ]; then
    log "\`ah\` already on PATH: $existing_ah (leaving it)"
  elif mkdir -p "$SHIM_DIR" 2>/dev/null; then
    cat > "$SHIM_DIR/ah" <<EOF2
#!/usr/bin/env sh
exec "$VENV_AH" "\$@"
EOF2
    chmod +x "$SHIM_DIR/ah"
    log "Installed \`ah\` shim to $SHIM_DIR/ah"
    case ":$PATH:" in
      *":$SHIM_DIR:"*)
        log "$SHIM_DIR already on PATH in this shell"
        PATH_STATE="on-path"
        ;;
      *) ensure_path_in_rc "$SHIM_DIR" ;;
    esac
  else
    warn "Could not write $SHIM_DIR/ah; activate the venv instead: source .venv/bin/activate"
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
if [ "$PATH_NOW" = "0" ] && [ "$PATH_STATE" != "shim-failed" ] && [ -f "$SHIM_DIR/ah" ]; then
  cat <<EOF

[action needed] \`ah\` is installed but not on PATH in THIS shell yet.
Run this one line now (new terminals pick it up automatically):
  export PATH="\$HOME/.local/bin:\$PATH"
EOF
  case "$PATH_STATE" in
    added:*|already:*) printf 'Persisted in %s — or just restart your terminal.\n' "${PATH_STATE#*:}" ;;
  esac
  echo ""
fi

if [ "$PATH_STATE" = "shim-failed" ]; then
  warn "No \`ah\` shim was installed; use the venv directly:"
  warn "  source \"$(pwd)/.venv/bin/activate\" && ah"
  echo ""
fi

cat <<EOF
Done. Next:
  ah                          # interactive terminal UI
  ah chat "What is the capital of France?"   # one-shot (needs OPENROUTER_API_KEY)
  ah serve --host 127.0.0.1 --port 8000      # HTTP API

Keys:   ah setup  (or /keys in the UI; values never echo)
Health: ah doctor
Docs:   $(pwd)/README.md
EOF
