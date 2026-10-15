#!/usr/bin/env bash
# R4-095 probe: why the compose ./machina mount is inert for the service.
# Models the two invocation styles against a cwd-local "shadow" package:
#   1. console script (what `machina init`/`serve` use): sys.path[0] is the
#      script's bin dir, so cwd (in the container: /app) is NOT searched and
#      the pip-installed copy wins.
#   2. `python -c`/`python -m` from cwd: sys.path[0] is '', so a cwd-local
#      package shadows the installed one (the mixed-code caveat documented in
#      docker-compose.yml).
set -u

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
VENV_PY="$REPO/.venv/bin/python"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/r4095-mount.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/app/machina" "$WORK/bin"
printf 'SHADOW = True\n' > "$WORK/app/machina/__init__.py"
cat > "$WORK/bin/console-probe" <<EOF
#!$VENV_PY
import sys

import machina

print("console script: sys.path[0] =", sys.path[0])
print("console script: machina.__file__ =", machina.__file__)
EOF
chmod +x "$WORK/bin/console-probe"

cd "$WORK/app"
printf '== 1. console-script style (machina init / machina serve) ==\n'
"$WORK/bin/console-probe"
printf '   shadow at %s/app/machina: %s\n' "$WORK" \
  "$(grep -q 'app/machina' <("$WORK/bin/console-probe") && echo USED || echo NOT USED)"

printf '\n== 2. python -c / python -m style from cwd (the caveat) ==\n'
"$VENV_PY" -c 'import sys; import machina; print("python -c: sys.path[0] =", repr(sys.path[0])); print("python -c: machina.__file__ =", machina.__file__)'

printf '\n== 3. resolution recap ==\n'
if "$WORK/bin/console-probe" | grep -q 'app/machina'; then
  echo "FAIL: console script imported the cwd shadow (compose note would be wrong)"
  exit 1
fi
echo "PASS: console script ignores the cwd shadow (installed copy wins)"
if "$VENV_PY" -c 'import machina; print(machina.__file__)' | grep -q 'app/machina'; then
  echo "PASS: python -c from cwd does see the shadow (caveat is real)"
  exit 0
fi
echo "FAIL: python -c did not see the cwd shadow"
exit 1
