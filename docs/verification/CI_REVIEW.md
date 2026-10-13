# CI ownership and verification — 2026-10-13

Recent GitHub runs failed at Ruff's import-order check with seven errors; the UI
and Docker jobs passed. Local reproduction also found formatting failures hidden
behind that first gate. Formatting/import fixes preserve the existing checks.

The main CI workflow retains its `lint-and-test`, `ui` and `docker` job IDs.
Lint, security scanning, the 60% coverage threshold, package validation/uploads
and Docker/Compose checks remain. The backend job no longer installs Node/UI
dependencies or optional LangGraph dependencies. It excludes `ui_integration`
and `workflow_trial` tests; the existing UI job owns the full UI check and real
gateway tests. Unfiltered local pytest still includes the UI wrapper.

The separate **Workflow research** workflow installs optional research extras
and runs the eight marked native/LangGraph checks. It triggers on relevant
research/dependency changes, Mondays at 08:00 UTC, and manual dispatch. It is
not a required replacement for any main CI job.

Local evidence on Python 3.11:

- Core environment contains neither LangGraph nor psycopg: **2,546 passed,
  one skipped, nine deselected**, **82.39% coverage** (135.72 seconds).
- Research selection: **eight passed**, 2,548 deselected.
- UI wrapper: **one passed**, 2,555 deselected; it executes the full UI
  typecheck/test/lint command and asserts no UI test skips.
- Collection partition: all **2,556** tests assigned to exactly one group,
  with no overlaps or unowned tests.
- Actionlint accepts both workflows. Ruff lint/format and `git diff --check`
  pass for the affected files and existing backend/tool scope.
- Before this split, the same checkout passed Bandit's medium/high gate and
  built/validated its wheel and source distribution; product changes here are
  limited to the earlier import-order and formatting corrections.

Independent read-only review found no material issues. These are local macOS
results, not a completed Ubuntu GitHub run. Changes have not been pushed.
