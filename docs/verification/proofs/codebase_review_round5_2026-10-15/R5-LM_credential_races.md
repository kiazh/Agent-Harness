# R5-LM — Credential publication/removal races (findings 16 + 17)

Lane files: `machina/security/copilot_auth.py`, `machina/security/providers.py`,
new `tests/test_r5_016_017_credential_races.py`. No commit (coordinator commits).

## L1 — Stale `gh` import overwrites/resurrects Copilot auth (finding 16)

RED (unfixed, real files + real flock, N=5/5 each — see
`logs/R5-LM_RED.txt`, all 15 failed):
- `test_l1_stale_import_must_not_resurrect_after_logout`: import blocked in
  the `gh` probe while `logout()` runs, then the stale probe result was
  published — the removed store was recreated (`gho_stale_import_race`).
  Reproduces the "recreates a store removed during logout" half.
- `test_l1_stale_import_must_not_overwrite_device_login`: a device login
  completing mid-import (`publish_device_token`) was overwritten by the
  stale import. Reproduces the "overwritten by the stale import" half.

Fix (`copilot_auth.py: import_from_gh_cli`): capture authentication
generation + store-content snapshot before the slow probe/account work
(same fencing the device path uses, mirroring ChatGPT `import_from_codex`);
publish under `store_lock` only when the generation is unchanged, the store
was not removed out of band, and (unless `force`) no credential appeared
meanwhile; post-save generation re-check undoes our own write when a logout
raced the gate (needed because `fenced_logout` deliberately takes no lock).
Stale in-flight work now returns `None` (never republishes revoked
credentials); a re-import issued *after* the logout/login captures the new
generation and still succeeds, so intentional re-import is preserved.
GREEN: 15/15, stable across 3 consecutive full runs.

Behavior/API notes: the existing-store check moved from before the probe to
under the publication lock, so `gh auth token` now runs even when the import
will be refused (required to close the TOCTOU); return contract unchanged
(stored dict / `None` / typed `CopilotAuthError`). `force=True` still
overwrites a peer change made after the call started (same semantics as the
ChatGPT Codex import).

## L2 — Provider removal deletes a replacement credential (finding 17)

RED (unfixed, N=5/5 — `logs/R5-LM_RED.txt`): remover blocked in
`remove_env_values` (the post-lock gap) while a re-add of the same provider
completed; the old removal then deleted the fresh `.env` line + process-env
var. Final state: provider registered, key gone everywhere
(`read_env_values`, `os.environ`, `resolve_provider_key` all empty).
Reproduces the finding exactly.

Fix (`providers.py: remove_provider`): store-entry removal AND credential
cleanup (`.env` line, process env, secret-cache invalidation) now share one
`_store_lock` hold — the same nesting `add_provider` already uses for
`set_env_values`, so no new lock ordering. Concurrent re-adds serialize to a
safe order (removal fully first, or the removal takes the fresh entry down
as the newest operation); the torn registered-but-keyless state is
unreachable. GREEN: 15/15 incl. the 5 L2 rounds.

Behavior/API notes: lock hold time extends across local `.env` I/O only
(still well under the 5 s bounded acquisition); cleanup report shape and
idempotency (`None` when absent) unchanged.

## Verification

- New: `tests/test_r5_016_017_credential_races.py` — 15 tests (3 races × 5),
  H-RACE style, real files + real `flock`, no DB involvement.
- RED log: `logs/R5-LM_RED.txt` (15 failed on unfixed code, via temporary
  stash of the two source files only — test file untouched).
- GREEN log: `logs/R5-LM_GREEN.txt` (15 passed, ×3 consecutive runs).
- Regressions: `test_copilot_auth.py`, `test_provider_store.py`,
  `test_r4_045_047_049_providers.py`, `test_r4_049b_fdclose.py`,
  `test_provider_custom.py`, `test_copilot_provider.py` — 97 passed.
