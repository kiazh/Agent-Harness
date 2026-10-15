# Fix evidence — gateway global `execution_mode` env shadow (F1) + durable revoke (F6)

Date: 2026-10-15
Lane: final review (FIX LANE — gateway mode).
Source findings: `docs/verification/proofs/final_review_2026-10-15/line_gateway_obs_cli.md` F1 (P2), F6 (P3).
Baseline: post-fix HEAD `1e1edbf` (line review), working-tree edits only — no commits.

## Files changed

| File | Change |
| --- | --- |
| `machina/gateway/features/mode.py` | New `sync_execution_mode_env()` (CLI `_sync_env_shadow` parity); `mode_set(scope="global")` and both `mode_revoke` global branches now sync the env/`.env` shadow; global revoke persists (`persist=True`). |
| `machina/gateway/server.py` | Env-sync touchpoints only: `_initialize --mode` (process-scoped sync) and the generic `config.set` bridge for `execution_mode` (process env always, `.env` on persist). |
| `tests/test_final_review_2026_10_15_gateway_mode.py` | New evidence test file (5 tests) — the RED/GREEN driver. |
| `docs/verification/proofs/final_review_2026-10-15/logs/fix_gateway_mode_*` | RED/GREEN/focused/ruff logs + diff referenced below. |

`machina/cli/__init__.py` was **not** touched; session-scoped mode semantics are unchanged. No other files were modified by this lane.

## Method

RED-first. The five new tests were written first and run against the unmodified
gateway (my two source files stashed to baseline) to confirm they fail on the
current defect, then the fix was applied and the same tests re-run GREEN,
followed by the focused suites and ruff.

- RED: `logs/fix_gateway_mode_RED.txt` → `5 failed` (all `assert config.get("execution_mode") == "ask"` observed `'full'`).
- GREEN: `logs/fix_gateway_mode_GREEN.txt` → `5 passed`.
- Focused: `logs/fix_gateway_mode_focused.txt` → `192 passed, 1 skipped` (Windows-only skip).
- Ruff: `logs/fix_gateway_mode_ruff.txt` → `ruff check` + `ruff format --check` both exit 0.
- Diff: `logs/fix_gateway_mode.diff`.

RED command (two source files temporarily at baseline):

```bash
git stash push -- machina/gateway/features/mode.py machina/gateway/server.py
.venv/bin/python -m pytest tests/test_final_review_2026_10_15_gateway_mode.py -p no:randomly -q -rA
git stash pop
```

GREEN + focused:

```bash
.venv/bin/python -m pytest tests/test_final_review_2026_10_15_gateway_mode.py -p no:randomly -q -rA
.venv/bin/python -m pytest tests/test_gateway.py tests/test_gateway_config_atomicity.py \
  tests/test_gateway_providers.py tests/test_features_split.py tests/test_audit_remediation.py \
  tests/test_fm1_regressions.py tests/test_session_mode_persistence.py \
  tests/test_integration_permissions.py tests/test_r4_093_cli_cluster.py \
  tests/test_final_review_cli_config_setup.py -p no:randomly -q -rs
```

## F1 — P2 — gateway global mode writes were shadowed by `MACHINA_EXECUTION_MODE`

**Defect.** `Config.get` reads `MACHINA_<KEY>` (env / `.env`) *before* the
in-memory attribute (`core/config.py:494-518`). The gateway global paths —
`mode_set(scope="global")` (`mode.py:66-70`), `mode_revoke(scope="global")`
(`mode.py:148-155`), the generic `config.set` bridge (`server.py:695-701`) and
`_initialize --mode` (`server.py:498-502`) — only called `Config.set`, which
never touches `os.environ`. The `.env` line `machina mode X` leaves behind
therefore silently won: a full→ask downgrade returned `{"mode": "ask"}` while
`config.get("execution_mode")` stayed `"full"` and `grant_full_default`
(`mode.py:260`) kept auto-granting full authority to every new session.

**Fix.** New helper mirrors the CLI's `_sync_env_shadow` (`cli/__init__.py:93-112`):

```python
def sync_execution_mode_env(mode: str, *, persist: bool) -> None:
    os.environ["MACHINA_EXECUTION_MODE"] = mode      # immediate effect (config.get)
    if not persist:
        return
    try:
        from machina.security.env_file import set_env_values
        set_env_values({"MACHINA_EXECUTION_MODE": mode})  # survive restarts
    except (OSError, ValueError) as error:
        logger.warning("execution_mode: .env sync failed for %s (%s)", ...)
```

Wired at every gateway global mode-write callsite:

- `mode_set(scope="global")` → `sync_execution_mode_env(mode, persist=persist)`.
- `mode_revoke` (DB and DB-free global branches) → `("ask", persist=True)`.
- `_config_set` for `key == "execution_mode"` → `sync_execution_mode_env(str(value), persist=persist)`.
  Uses the **coerced new value**, never `config.get(key)` (which still prefers the
  stale shadow — the same F09 lesson the CLI fix records).
- `_initialize --mode` → `sync_execution_mode_env(mode, persist=False)`: the
  launcher's `--mode` is documented as "applied to this gateway process only
  (non-persistent)" (`server.py:495-497`), so it updates the process env only and
  never writes `.env`.

**Design note — process env always, `.env` on `persist`.** A global write must
change the *effective* value immediately, so the process environment is always
updated. The `.env` file is written only on the durable (`persist=True`) path so
a non-persistent write is not silently promoted into a restart-durable one —
consistent with the existing `model`/`provider` gateway sync
(`server.py:710-730`) and the existing persist semantics pinned by
`test_mode_set_persist_false_string_is_not_truthy`. The CLI always passes
`persist=True`, so the CLI's observable behavior is matched exactly.

**RED → GREEN.** `test_global_mode_set_beats_preexisting_env_shadow`,
`test_global_mode_set_persist_writes_env_shadow`,
`test_generic_config_set_execution_mode_beats_env_shadow` and
`test_initialize_mode_beats_env_shadow` fail at baseline (`config.get` still
`'full'`) and pass after the fix.

**Behavior change (call out).** A gateway global `mode.set` / `config.set
execution_mode` now also updates the `MACHINA_EXECUTION_MODE` process env (and,
on `persist=True`, `.env`). This is the intended parity with `machina mode X`
and is exactly what makes a downgrade effective. Session-scoped
(`scope="session"`) writes are untouched.

## F6 — P3 — global `mode.revoke` was not durable

**Defect.** Both `mode_revoke` global branches called
`config.set("execution_mode", "ask")` with the default `persist=False`
(`mode.py:150,154`), contradicting the docstring's "reset the persistent
default" and the CLI's durable `config.set(..., persist=True)` reset
(`cli/__init__.py:563`). The running default changed in memory only; a restart
re-read the old `execution_mode` from `config.yaml`.

**Fix.** Both branches now `config.set("execution_mode", "ask", persist=True)`
and `sync_execution_mode_env("ask", persist=True)`, so the YAML default and the
`.env` shadow are both reset.

**RED → GREEN.** `test_global_mode_revoke_is_durable` fails at baseline
(`config.get` stayed `'full'`; no `config.yaml` written) and passes after the
fix (YAML `execution_mode: ask`, `.env` shadow `ask`, effective `ask`).

## Verification summary

| Check | Result |
| --- | --- |
| New evidence tests (baseline, stashed sources) | 5 failed (RED) |
| New evidence tests (fixed) | 5 passed |
| `test_gateway` / `test_gateway_config_atomicity` / `test_gateway_providers` | passed |
| `test_features_split` (mode.revoke registration) | passed |
| `test_audit_remediation` (mode global set) | passed |
| `test_fm1_regressions` (persist truthiness) | passed |
| `test_session_mode_persistence` (session mode unchanged) | passed |
| `test_integration_permissions` (`_initialize --mode`) | passed (1 Windows-only skip) |
| `test_r4_093_cli_cluster` (CLI neighbor) | passed |
| `test_final_review_cli_config_setup` (CLI F09 neighbor) | passed |
| Focused suites total | 192 passed, 1 skipped |
| `ruff check` + `ruff format --check` (3 files) | All checks passed (exit 0) |

Unrelated, pre-existing: `tests/test_progress_monitor.py`,
`tests/test_replay_harness.py`, `tests/test_final_review_connectors_fixes.py`
fail at **collection** because `machina/core/progress.py` (modified by a
different lane) no longer defines `should_write_first`. Not on this lane's
files; excluded from the focused run.

## Per-finding status

- **F1** — fixed (global `mode.set` / `mode.revoke` / generic `config.set` /
  `_initialize --mode` all keep the effective value in step with the request);
  RED→GREEN demonstrated; focused suites + ruff green.
- **F6** — fixed (global `mode.revoke` persists `ask` durably, CLI parity);
  RED→GREEN demonstrated; focused suites + ruff green.
