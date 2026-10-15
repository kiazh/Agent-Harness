# Fix lane — final-review small/misc findings (2026-10-15)

Lane: **FIX LANE (final review) — misc/small**. Red-first where feasible;
smallest safe change; typed/deliberate errors; no commits.
Baseline for the review reports: `1e1edbf` (worktree also carries other
parallel fix lanes; this lane only touched the files listed below).

## Files changed

| File | Change |
|---|---|
| `machina/mcp_server/__init__.py` | S2: clamp an omitted `mode` to the MCP ceiling when the ceiling is tightened below the operator mode. |
| `tools/e2e_gate.py` | F1: a blank `--db-url` is normalized to absent (guard **and** export path). |
| `machina/core/progress.py` | Delete the dead `should_write_first` + `MIN_EVIDENCE_TO_WRITE_FIRST` surface. |
| `machina/skills/registry.py` | S5/F3: symlink-safe `mkstemp`+`O_EXCL`+fsync+replace skill rewrite. Addendum: one canonical (casefolded) key for the in-memory skill map. |
| `machina/security/providers.py` | P2-2: `_store_lock` delegates to `store_fence.store_lock`; delete the duplicate `_acquire_lock` + private flock constants. |
| `machina/core/child_process.py` | F14: start deadline supervision + output draining **before** delivering stdin; deliver stdin on a bounded daemon writer thread. |
| `FEATURE_LEDGER.md:217` | F2 wording: `minimal` is a distinct catalog tier, not "alias-normalized". |
| `tests/test_delegation_contracts.py` | Remove the vacuous `should_write_first` spies; keep the real behavioral asserts. |
| `tests/test_progress_monitor.py` | Drop the deleted helper's test + import. |
| `tests/test_replay_harness.py` | Drop the deleted helper's import/assert. |
| `tests/test_r5_019_skill_atomicity.py` | Inject the write failure at the new publish seam (`os.replace`) instead of `Path.write_text`. |
| `tests/test_r4_045_047_049_providers.py` | Contended-lock test overrides the shared `store_fence.STORE_LOCK_TIMEOUT_SECONDS` (the bound now comes from the shared helper). |
| `tests/test_final_review_fixes_2026_10_15.py` | **NEW** RED-first evidence/regression suite (11 tests). |

## Per-finding status

### 1. S2 (security_review) — MCP mode-ceiling bypass on default mode — FIXED

`machina_chat` computed the ceiling only when `mode` was supplied. With
`MACHINA_EXECUTION_MODE=full` + `MCP_MAX_MODE=ask`, `mode=None` spawned
`machina chat -q -- <prompt>` with no `--mode`, so the child inherited `full`
above the declared ceiling. Now the ceiling is always computed: a requested
mode above it is still refused (unchanged), and an omitted mode is clamped to
the ceiling **only when the ceiling is below the operator mode** (`--mode ask`
in the example). When the ceiling equals the operator mode, no flag is added
(unchanged). RED→GREEN in
`test_mcp_none_mode_clamped_to_tightened_ceiling` /
`test_mcp_none_mode_without_tightening_inherits_operator_default`.

### 2. F1 (line_infra_docs) — blank `--db-url` defeats the guard — FIXED

`--db-url=` stored `""`; `args.db_url is None` was False, so the non-test-DB
refusal was skipped while the export path still ran. `main()` now normalizes
once (`explicit_db_url = args.db_url if (args.db_url and args.db_url.strip())
else None`) and gates both the guard and the export on it. RED→GREEN in
`test_e2e_gate_blank_db_url_is_not_consent` (refused) and
`test_e2e_gate_blank_db_url_with_test_db_still_proceeds` (a test-named DB is
not refused; the run stops later at the bogus venv, so no journeys execute).

### 3. P3 (code_review #7 / line_ui) — dead `should_write_first` — DELETED

No production caller after R4-028. Deleted the function and its only constant,
plus the vacuous `tests/test_delegation_contracts.py` spies
(`assert_not_called()` on a function nothing calls), the helper's unit test in
`test_progress_monitor.py`, and its assert in `test_replay_harness.py`. The
real behavioral asserts in those tests are kept.

### 4. S5 (security_review) / F3 (adversarial) — predictable, symlink-following skill temp — FIXED

`_write_skill_file` wrote `SKILL.md.<pid>.tmp` via `Path.write_text`, which
follows a pre-planted symlink. It now uses `tempfile.mkstemp` (unpredictable
name, `O_EXCL`) in the skill's own directory, `fchmod` 0600, write + flush +
`fsync`, then `os.replace`; failure unlinks the temp and re-raises. RED→GREEN in
`test_write_skill_file_ignores_a_planted_symlink_temp` (planted
`SKILL.md.<pid>.tmp` → outside file stays `ORIGINAL`).

### 5. P2-2 (code_review #2) — provider store lock duplicated `store_fence` — FIXED

`_store_lock`/`_acquire_lock` reimplemented the bounded flock. `_store_lock` now
delegates to `store_fence.store_lock(providers_path())` and maps
`store_fence.StoreLockTimeout` → `ProviderStoreLockTimeout` (typed error
unchanged; call sites unchanged). `_acquire_lock`, `_WINDOWS`, and the private
`fcntl`/`msvcrt` imports are removed; `_STORE_LOCK_TIMEOUT_SECONDS` is now an
alias of the shared `store_fence.STORE_LOCK_TIMEOUT_SECONDS`.

**Timeout tradeoff:** the *default* bound moves from the old private 5 s to the
shared `STORE_LOCK_TIMEOUT_SECONDS` (45 s) — the direct consequence of the
"thin wrapper" delegation the reviewer recommended (and the same pattern
`chatgpt_auth._store_lock` already uses). Preserving 5 s would require
parameterizing `store_fence.store_lock`, which is outside this lane's file
ownership. The call-site semantics (whole load→mutate→replace under one lock)
and the typed `ProviderStoreLockTimeout` are unchanged; the RED-first tests
prove the delegation and the typed translation. RED→GREEN in
`test_provider_store_lock_delegates_to_store_fence` and
`test_provider_store_lock_translates_store_fence_timeout`.

### 6. F14 (follow-up, P3) — bounded runner writes stdin before supervision — FIXED

`run_bounded` wrote **all** stdin synchronously before starting the deadline
timer or the output readers, so a large input could block indefinitely (and a
chatty child could deadlock both directions). The writes now happen on a daemon
writer thread started **after** the timer and both capped readers are live; the
kill-tree on expiry/cancel closes the child's read end, so a blocked write fails
instead of hanging the call. The writer is joined (bounded, 1 s) before
`process.wait()`. RED→GREEN in
`test_run_bounded_large_stdin_is_killed_at_the_deadline` (1 MB into a child that
never reads, `timeout=1.0` → `timed_out`, < 3 s) and
`test_run_bounded_delivers_large_stdin_when_the_child_reads` (500 kB echoed
length).

### 7. F2 (line_infra_docs) — `minimal` ledger wording — FIXED

`FEATURE_LEDGER.md:217` now reads
``accepted (distinct catalog tier, not alias-normalized)``; config stores
`minimal` verbatim like `xhigh|max`, and only the unset aliases normalize.

### 8. Addendum P3 (line_rag_tools_skills) — case-insensitive collision desync — FIXED

`load_all` looked skills up with `skill.name.casefold()` but stored under the
raw `skill.name`, so a builtin `Foo` and a local `FOO` occupied two keys and the
collision guard missed. The in-memory map is now keyed by one canonical spelling
(`_skill_key` = `casefold`) at every store/lookup/delete site (`load_all`,
`create_skill`, `get`, `get_skill_content`, `record_use`/`record_view`,
`get_telemetry`, `get_provenance`, `update_skill`, `set_enabled`,
`delete_skill`). Lowercase-name tests stay green. RED→GREEN in
`test_registry_lookup_is_case_insensitive` and
`test_uppercase_local_skill_cannot_duplicate_a_builtin` (upper-case local `FOO`
no longer duplicates builtin `Foo`).

## Verification

RED (before the fixes; 8 failed / 3 passed — the 3 are positive controls):

```
.venv/bin/python -m pytest tests/test_final_review_fixes_2026_10_15.py -q
# 8 failed, 3 passed  -> logs/red_new_fixes.txt
```

GREEN (after):

```
.venv/bin/python -m pytest tests/test_final_review_fixes_2026_10_15.py -q
# 11 passed -> logs/green_new_fixes.txt

# Focused review suites (mcp_server, e2e-gate paths, delegation contracts,
# skills registry/repo_install, provider store, child_process/interop, plus
# adjacent skill/terminal suites):
.venv/bin/python -m pytest <25 focused files> -q
# 626 passed -> logs/green_focused.txt

.venv/bin/ruff check <changed files>
# All checks passed!  -> logs/ruff_final_review.txt
.venv/bin/ruff format --check <changed files>
# 12 files already formatted
```

Known-unrelated: `tests/test_architecture_layers.py` has 3 line-ceiling
failures caused by **other lanes'** edits to `machina/core/provider/{audit,sse}.py`
(files this lane did not touch). Everything this lane changed passes.

## Residuals / notes

- The provider-store default lock bound is now the shared 45 s (see finding 5);
  flagged for reviewer awareness. No other behavior changed.
- The `_write_skill_file` rewrite now leaves the replaced `SKILL.md` at 0600
  (mkstemp + `fchmod`), matching the store-writer idiom rather than the prior
  umask-derived mode.
