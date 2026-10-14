#!/bin/bash
# Audit finding: a relative MACHINA_ENV_FILE is resolved for installer writes
# but never exported as its absolute result; the child CLI (doctor/init, run
# after the installer cd'd into APP_DIR) resolves a different file.
set -eu
REPO=/Users/shadow/machina
START_DIR=$(mktemp -d "${TMPDIR:-/tmp}/audit-start.XXXXXX")
APP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/audit-app.XXXXXX")
echo "START_DIR_MARKER" > "$START_DIR/rel.env"
echo "APP_DIR_MARKER" > "$APP_DIR/rel.env"

cd "$START_DIR"
export MACHINA_ENV_FILE=rel.env
# Real installer resolver (tools/install-helpers.sh).
installer_target=$(bash -c '. /Users/shadow/machina/tools/install-helpers.sh; resolve_env_file "$1"' _ "$START_DIR")
echo "installer resolved target: $installer_target"

# Real child-CLI resolution performed from APP_DIR after the installer's cd.
cd "$APP_DIR"
child_target=$(HOME="$APP_DIR/home" /Users/shadow/machina/.venv/bin/python -c \
  'from machina.security.env_file import find_env_file; print(find_env_file())')
echo "child CLI resolved target:   $child_target"

if [ "$installer_target" != "$child_target" ]; then
  echo "CLAIM CONFIRMED (installer and child CLI target different files)"
  rm -rf "$START_DIR" "$APP_DIR"
  exit 0
else
  echo "CLAIM NOT REPRODUCED"
  rm -rf "$START_DIR" "$APP_DIR"
  exit 1
fi
