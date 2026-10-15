#!/usr/bin/env bash
# R4-095 checks transcript: static verification for the infra cluster.
# Read-only checks only (bash -n, YAML/TOML parse, focused pytest, ruff,
# offline uv lock check). No docker, no installers, no network.
set -u
cd "$(cd "$(dirname "$0")/../../../.." && pwd)"

sec() { printf '\n$ %s\n' "$*"; }

sec "git status --short"
git status --short

sec "bash -n install.sh tools/install-helpers.sh"
bash -n install.sh && echo "install.sh: syntax OK"
bash -n tools/install-helpers.sh && echo "install-helpers.sh: syntax OK"

sec "python -c yaml/toml parse (workflows, dependabot, compose, pyproject)"
.venv/bin/python - <<'PY'
import tomllib
import yaml
for path in (
    ".github/workflows/ci.yml",
    ".github/workflows/workflow-trial.yml",
    ".github/dependabot.yml",
    "docker-compose.yml",
):
    with open(path, encoding="utf-8") as fh:
        yaml.safe_load(fh)
    print(f"YAML OK: {path}")
with open("pyproject.toml", "rb") as fh:
    tomllib.load(fh)
print("TOML OK: pyproject.toml")
PY

sec "actionlint availability"
command -v actionlint || echo "actionlint: not installed on this machine (YAML parse above is the substitute)"

sec "uv lock --check --offline (local, read-only; CI runs the online form)"
cp uv.lock "$TMPDIR/uv.lock.r4095.before" 2>/dev/null || cp uv.lock /tmp/uv.lock.r4095.before
uv lock --check --offline
cmp uv.lock "${TMPDIR:-/tmp}/uv.lock.r4095.before" && echo "uv.lock unchanged"

sec "focused pytest (tests/test_release_artifacts* absent -> skipped per task)"
ls tests/test_release_artifacts* 2>/dev/null || echo "tests/test_release_artifacts*: absent"
.venv/bin/python -m pytest tests/test_installer_env.py tests/test_infra_fixes.py tests/test_vendor_ecc.py \
  -q -p no:cacheprovider

sec "windows-only test reports expected skip (on Linux CI the step greps '1 skipped')"
.venv/bin/python -m pytest \
  "tests/test_integration_permissions.py::test_scope_windows_case_unc_and_symlink_lexical" \
  -q -p no:cacheprovider -rs

sec "related config/vendor tests"
.venv/bin/python -m pytest \
  "tests/test_final_pass_fixes.py::test_docker_context_and_docs_handle_telemetry_state" \
  "tests/test_audit_regressions.py::test_compose_starts_api_and_initializes_schema" \
  "tests/test_security.py::TestNoExposedDatabasePort" \
  "tests/test_ecc_agents.py::test_all_pinned_ecc_agents_and_only_agents_are_bundled" \
  -q -p no:cacheprovider

sec "ruff check / format on tools/"
.venv/bin/python -m ruff check tools/
.venv/bin/python -m ruff format --check tools/

sec "probe scripts (captured outputs live next to this transcript)"
ls -1 docs/verification/proofs/codebase_review_round4_2026-10-14/R4-095_*
