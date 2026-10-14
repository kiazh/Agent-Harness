#!/bin/bash
# GREEN check for audit_installer_env.sh: the repro itself hardcodes the
# pre-fix step (export MACHINA_ENV_FILE=rel.env) and never runs install.sh, so
# it can only confirm the hazard. This check replays the installer's exact
# sequence using the REAL export line extracted from install.sh, the real
# resolve_env_file, and the real child-CLI find_env_file, and asserts both
# target the same file.
set -eu
REPO=/Users/shadow/machina
INSTALL_SH="$REPO/install.sh"

# 1. The fix must exist and must run before init (line ~341) and doctor (~377).
export_line=$(grep -n '^export MACHINA_ENV_FILE=' "$INSTALL_SH" | head -1 | cut -d: -f1)
resolve_line=$(grep -n '^ENV_FILE="\$(resolve_env_file' "$INSTALL_SH" | head -1 | cut -d: -f1)
init_line=$(grep -n '"$MACHINA_BIN" init\|try_init 2>&1' "$INSTALL_SH" | head -1 | cut -d: -f1)
doctor_line=$(grep -n '"$MACHINA_BIN" doctor' "$INSTALL_SH" | head -1 | cut -d: -f1)
[ -n "$export_line" ] || { echo "FAIL: no export MACHINA_ENV_FILE in install.sh"; exit 1; }
[ "$resolve_line" -lt "$export_line" ] || { echo "FAIL: export before resolution"; exit 1; }
[ "$export_line" -lt "$init_line" ] || { echo "FAIL: export after init"; exit 1; }
[ "$export_line" -lt "$doctor_line" ] || { echo "FAIL: export after doctor"; exit 1; }
echo "ordering: resolve line $resolve_line < export line $export_line < init $init_line / doctor $doctor_line"

# 2. Replay the real flow: relative override resolves against START_DIR, the
#    real export line is applied, then the child CLI resolves from APP_DIR.
START_DIR=$(mktemp -d "${TMPDIR:-/tmp}/green-start.XXXXXX")
APP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/green-app.XXXXXX")
trap 'rm -rf "$START_DIR" "$APP_DIR"' EXIT
echo "START_DIR_MARKER" > "$START_DIR/rel.env"
echo "APP_DIR_MARKER" > "$APP_DIR/rel.env"

cd "$START_DIR"
export MACHINA_ENV_FILE=rel.env
# shellcheck disable=SC1091
. "$REPO/tools/install-helpers.sh"
ENV_FILE="$(resolve_env_file "$START_DIR")"
# Apply install.sh's actual export statement, verbatim.
eval "$(sed -n "${export_line}p" "$INSTALL_SH")"
echo "installer resolved+exported: $MACHINA_ENV_FILE"

cd "$APP_DIR"
child_target=$(HOME="$APP_DIR/home" "$REPO/.venv/bin/python" -c \
  'from machina.security.env_file import find_env_file; print(find_env_file())')
echo "child CLI resolved target:  $child_target"

# macOS /var vs /private/var (and the TMPDIR trailing slash) are the same file:
# compare canonical paths.
canon() { "$REPO/.venv/bin/python" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"; }
if [ "$(canon "$ENV_FILE")" = "$(canon "$child_target")" ]; then
  echo "CLAIM NOT REPRODUCED (installer and child CLI agree: $(canon "$child_target"))"
  exit 0
fi
echo "STILL BROKEN (installer='$ENV_FILE' child='$child_target')"
exit 1
