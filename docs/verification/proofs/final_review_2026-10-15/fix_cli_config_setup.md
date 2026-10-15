# Fix evidence — CLI `config-set` env shadow (F09) + `setup --force` replace (F10)

Date: 2026-10-15
Lane: final review (FIX LANE).
Source findings: `docs/verification/CODEBASE_REVIEW_FOLLOWUP_2026-10-15.md` F09, F10 (both P2).
Baseline commit: `1e1edbf`.

## Files changed

| File | Change |
| --- | --- |
| `machina/cli/__init__.py` | F09 `config_set` syncs the validated new value; F10 `setup --force` replaces the `.env` atomically at 0600 |
| `tests/test_final_review_cli_config_setup.py` | New evidence test file (6 tests) covering both findings + two no-regression guards |
| `docs/verification/proofs/final_review_2026-10-15/logs/*` | RED/GREEN/ruff logs referenced below |

No other files were modified. No commits.

## Method

RED-first: the new tests were written and run against the untouched CLI (baseline) to
confirm they fail on the current defects, then the CLI was changed and the same tests
re-run GREEN, followed by the focused suites and ruff.

- RED log: `logs/fix_cli_config_setup_red.txt` → `4 failed, 2 passed`.
- GREEN log: `logs/fix_cli_config_setup_green.txt` → new tests `6 passed`; focused suites
  `160 passed`.
- Ruff: `logs/fix_cli_config_setup_ruff.txt` → `All checks passed!` (exit 0).

Command (RED, CLI stashed to baseline):
`.venv/bin/python -m pytest tests/test_final_review_cli_config_setup.py -p no:cacheprovider -q -rA`

Command (GREEN):
`.venv/bin/python -m pytest tests/test_final_review_cli_config_setup.py -p no:cacheprovider -q -rA`
`.venv/bin/python -m pytest tests/test_fm7_cli.py tests/test_setup_secrets.py tests/test_r4_093_cli_allowlist.py tests/test_r4_093_cli_cluster.py tests/test_installer_env.py tests/test_installer_quoting.py tests/test_wave3_hotfix.py tests/test_env_file_adversarial.py tests/test_provider_store.py -p no:cacheprovider -q`

## F09 — P2 — persistent CLI model/provider change wrote the old value back

**Defect.** After `config.set(key, value, persist=True)`, the model/provider branch did
`str(config.get(key))`. `Config.get` returns `MACHINA_<KEY>` from the environment
*before* the just-saved attribute, so with `MACHINA_MODEL=old` present,
`machina config-set model new --persist` saved YAML `new` and then wrote `old` into
`os.environ` and `.env`, printing success.

**Fix** (`machina/cli/__init__.py`, `config_set`):

```python
_sync_env_shadow(key, str(validate_value(key, value)))
console.print(f"[dim]Effective {escape(key)}: {escape(str(config.get(key)))}[/dim]")
```

The shadow is now written from the *validated value just assigned*
(`validate_value`, the same coercion `Config.set` applied), and the effective value is
read via `config.get` only *after* the environment shadow is updated, so it now reports
the new value. `validate_value` was added to the existing top-level config import.

**RED → GREEN.** `test_config_set_model_persist_overrides_stale_env`,
`test_config_set_provider_persist_overrides_stale_env` fail at baseline
(`os.environ["MACHINA_MODEL"] == "old-model"`, `.env` = `old-model`) and pass after the
fix. `test_config_set_model_persist_without_env_still_syncs` guards the ordinary path.

**Behavior change (call out):**
- The `.env`/env shadow now carries the new value (the intended behavior); previously it
  could carry the stale env value.
- One extra muted confirmation line is printed on `--persist`:
  `Effective <key>: <value>`. No existing test asserts exact output for this command;
  substrings in `tests/test_r4_093_cli_cluster.py` / `tests/test_fm7_cli.py` still hold.
- Non-persisted `config-set` and non-model/provider keys are unchanged (no shadow write).

## F10 — P2 — `setup --force` merged the old environment instead of replacing it

**Defect.** `setup --force` printed "Overwriting existing .env" but only reset its
in-memory `existing` map. Because the file still existed, the `if not target.is_file()`
seed branch was skipped and `set_env_values` *merged* into the original file: prior
provider keys, `MACHINA_MODEL`/`MACHINA_PROVIDER` and other settings survived. Blank
interactive answers could not remove old secrets.

**Fix** (`machina/cli/__init__.py`, `setup`):

- Introduced `overwriting = target.is_file() and force`.
- For `overwriting`, build a fresh seed (comments/docs from `.env.example`, else the
  header line) and write it with the shared `env_file._write_env_lines` writer — an
  atomic `os.replace` at 0600 (`mkstemp` + `fchmod`) — *before* applying the new values.
  The subsequent `set_env_values(updates, target)` then merges the new values into the
  fresh file, so nothing prior survives.
- Ordinary (non-forced) setup keeps merge semantics unchanged.
- New output branch: forced setup with no updates prints
  `✓ Replaced <path> with a fresh .env.` instead of "No changes written".

Reusing the shared writer keeps the secret-file discipline (unpredictable
`mkstemp`, 0600 via `fchmod`, same-filesystem `os.replace`, directory fsync) rather than
re-implementing it. Importing the underscore helper follows the existing precedent in
`machina/gateway/features/secrets.py` (`from machina.security.env_file import _validate_key`).

**RED → GREEN.** `test_setup_force_noninteractive_replaces_env` fails at baseline
(`OPENROUTER_API_KEY=sk-or-old`, `MACHINA_MODEL=old-model`, `MACHINA_PROVIDER=old-provider`
all survive) and passes after the fix, with `DATABASE_URL` reset to the default and the
file at `0600`. `test_setup_force_interactive_blank_answers_remove_secrets` fails at
baseline on `OPENROUTER_API_KEY` surviving blank hidden input, and passes after.
`test_setup_without_force_still_merges` guards that ordinary setup does not wipe values.

**Behavior change (call out):**
- `setup --force` now truly discards all prior `.env` keys (including secrets); this is
  the documented contract ("`--force` starts over").
- Forced non-interactive setup still writes just the seeded `DATABASE_URL` (unchanged
  value semantics), but into a fresh file.
- Forced interactive setup writes the accepted provider/model defaults and any entered
  keys; blank hidden answers now remove prior secrets instead of keeping them.
- Fresh file is always 0600 (previously a forced existing-file path could retain the old
  file's mode).

## Verification summary

| Check | Result |
| --- | --- |
| New evidence tests (baseline) | 4 failed, 2 passed (RED) |
| New evidence tests (fixed) | 6 passed |
| `test_fm7_cli` | passed |
| `test_setup_secrets` | passed |
| `test_r4_093_cli_allowlist` / `test_r4_093_cli_cluster` | passed |
| `test_installer_env` / `test_installer_quoting` | passed |
| `test_wave3_hotfix` / `test_env_file_adversarial` / `test_provider_store` | passed |
| Focused suites total | 160 passed |
| `ruff check machina/cli/__init__.py tests/test_final_review_cli_config_setup.py` | All checks passed |

Note: two `test_production_readiness_p1.py` DB tests (`test_document_count_distinct_sources`,
`test_compress_outcome_statuses`) hit an `asyncpg` deadlock when run inside a large mixed
DB batch; both pass in isolation with and without this change (CLI-only diff, no DB
contact). The deadlock is environmental cross-test DB contention, not a regression.

## Per-finding status

- **F09** — fixed, RED→GREEN demonstrated, focused suites green.
- **F10** — fixed, RED→GREEN demonstrated, focused suites green.
