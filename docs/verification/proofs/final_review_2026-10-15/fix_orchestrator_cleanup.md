# F12 fix — delegation bookkeeping failure no longer leaks child/provider

Date: 2026-10-15
Lane: FIX (final review). Sole-writer files: `machina/core/orchestrator.py`,
`machina/core/agent_factory.py`, one new evidence test file. No commits.

## Findings addressed

- **F12** — `docs/verification/CODEBASE_REVIEW_FOLLOWUP_2026-10-15.md`:
  `Orchestrator.delegate` created the child session + owned provider, then
  awaited `_record_start` **outside** the execution cleanup `try/finally`.
  A DB error (or cancellation) during that insert skipped cleanup: the owned
  provider was never closed and the fresh child session was left active.
- **F4** — `docs/verification/proofs/final_review_2026-10-15/line_core_runtime.md`:
  `agent_factory.py` called `get_provider(...)` before unguarded steps
  (`MachinaAdapter.to_soulspec(...)` / `ReActAgent(...)`), so a raise there
  leaked the provider (HTTP client) with no `try/finally` to close it.

## Root cause

`orchestrator.py`: the build-failure guard (`except BaseException: archive`) and
the execution `finally` (reset contextvars, join background tasks, close owned
provider) were two separate scopes. `_record_start` sat between them, so
neither ran on its failure.

`agent_factory.py`: ownership (`agent._owns_provider = True`) is only stamped
after `ReActAgent` is constructed. Anything raising between
`llm = get_provider(...)` and that point left the provider unowned and unclosed.

## Fix (smallest safe change)

### `machina/core/orchestrator.py`

Collapsed the two scopes into one ownership scope that opens the moment the
child session + provider exist:

- `_record_start` is now inside the guarded `try`.
- `depth_token` / `deadline_token` / `authority_token` are initialised to
  `None` and the `finally` resets only those actually set.
- New `message_id is None` guard in `except Exception` and on the pre-run
  failure paths: a build or start-recording failure re-raises (no start row).
- The `finally` now, in order: resets contextvars (if set), joins background
  tasks, closes the owned provider (unchanged owner-aware contract), and — only
  when `message_id is None` (child never ran) — archives the fresh child
  session.
- Provider close + background join stay exactly as before for the normal and
  child-error paths; the success/error/cancel message recording and
  `DelegationResult` values are byte-for-byte the same.

Normal path unchanged: `_record_start` is still issued before the child runs
and `message_id` is still passed to `_record_end`. A completed child session is
still kept active (archive only fires when no start row was ever written).

### `machina/core/agent_factory.py`

Wrapped definition export + agent construction in a `try/except BaseException`
that closes the provider on failure, then added `_close_orphaned_provider`:

```python
async def _close_orphaned_provider(provider, timeout: float = 5.0) -> None:
    ...
    await close_agent_provider(
        SimpleNamespace(provider=provider, _owns_provider=True), timeout
    )
```

This reuses the audited owner-aware close contract (`close_agent_provider`):
bounded, sanitized reporting, close failures reported but never masking the
construction error, cancellation still propagating.

## Evidence test file

`tests/test_final_review_2026_10_15_cleanup.py` (new, sole evidence file):

1. `test_record_start_db_error_closes_provider_and_archives_child` — default
   factory path; `_record_start` raises `RuntimeError("db down")`; asserts the
   owned provider's `close()` was awaited once **and** the child session was
   archived.
2. `test_record_start_cancellation_closes_provider_and_archives_child` —
   same, with `_record_start` raising `asyncio.CancelledError`.
3. `test_factory_closes_provider_when_soulspec_step_raises` — patches
   `MachinaAdapter.to_soulspec` to raise; asserts the provider obtained from
   `get_provider` is closed.

All three fail on the pre-fix code (provider `closed == 0`, archive not called)
and pass after the fix.

## Verification

Logs in `docs/verification/proofs/final_review_2026-10-15/logs/`:

| Log | Result |
| --- | --- |
| `fix_orchestrator_cleanup_RED.txt` | 3 failed (pre-fix, injected failure) |
| `fix_orchestrator_cleanup_GREEN.txt` | 3 passed |
| `fix_orchestrator_cleanup_focused_suites.txt` | 120 passed |
| `fix_orchestrator_cleanup_ruff.txt` | `ruff check` all passed; `ruff format --check` 3 files already formatted |

Focused suites: `test_review_delegation`, `test_delegate_scope`,
`test_delegation_contracts`, `test_delegation_outcomes`,
`test_delegation_boundaries`, `test_delegation_transport`,
`test_delegate_validator`, `test_failed_delegation_usage`,
`test_orchestrator_tokens`, `test_orchestrator_usage`,
`test_fm4_orchestrator`, `test_multi_agent`, `test_agent_factory_memory`,
`test_final_review_2026_10_15_cleanup` → **120 passed**.

Additional regression sweep (factory/orchestrator/soulspec/permissions):
`test_authority_merge`, `test_soulspec_runtime`, `test_provider_close_visibility`,
`test_phase5_completion`, `test_ecc_agents`, `test_deadlock_guard`,
`test_deep_runtime_cleanup`, `test_handoff_regressions`,
`test_integration_permissions`, `test_bug_fixes`, `test_audit_regressions`
→ **160 passed, 1 skipped**.

## Verdict

**F12 and factory-F4 fixed and verified.** Failure of the start-row DB insert
(or cancellation during it) now closes the owned provider and archives the
child session; a raise after provider creation in the factory closes the
provider. Normal-path message recording and results are unchanged. No other
files touched.
