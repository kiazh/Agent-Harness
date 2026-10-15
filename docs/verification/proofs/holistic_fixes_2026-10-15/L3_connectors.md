# L3 — connectors / OAuth holistic fixes (H13, H15, H16, H17)

Date: 2026-10-15
Scope: `machina/connectors/**` + `tests/test_connectors_holistic_fixes.py` only.
Rules honored: RED-first reproduction per finding; smallest safe fixes; no commit/push;
no files touched outside ownership.

Test command (all runs):

```bash
MACHINA_TEST_DATABASE_URL='postgresql://postgres:postgres@localhost:5432/machina_test' \
  .venv/bin/python -m pytest <files> -p no:cacheprovider
```

## Result summary

| Finding | Reproduced (pre-fix) | Fix | Regression test |
|---|---|---|---|
| H13 | YES — `TimeoutError` after 2s/5s: real `asyncio.Lock` held by the dispatch, preflight re-entered `service.refresh()` | config/auth snapshot threaded through the OAuth preflight and bearer adapter; `ensure_token(..., config=...)` never re-resolves through the lock-taking service | `test_oauth_preflight_never_reenters_service_refresh_under_config_lock`, `test_authorized_oauth_connect_completes_preflight_under_config_lock` |
| H15 | YES — `TimeoutError`: 4 quiet drains starved a 2-worker default executor | `_PipeStderr` waits on `loop.add_reader` readiness with non-blocking reads; executor fallback only where the loop cannot watch raw fds | `test_quiet_stderr_drains_never_occupy_the_shared_executor` |
| H16 | YES — `AssertionError`: cancelled flow's credential was loadable after `cancel()` returned `True` | `PublicationFence` linearized with the store commit lock: cancel fences before cancelling and retires a published grant via CAS; `forget`/`invalidate_connector` fence too | `test_cancel_fences_in_flight_credential_publication` |
| H17 | YES — `OSError` propagated after the rename, envelope deleted while the index referenced it | `save` reconciles the visible index before rollback cleanup; a referenced envelope is treated as a committed publication | `test_index_dir_fsync_failure_keeps_referenced_envelope` |

Pre-fix (RED) evidence: `logs/L3_red_prefix.log` — all 5 tests fail for the stated reasons
(two `TimeoutError` deadlocks, one `TimeoutError` starvation, one post-cancel publication
assertion, one injected-fsync `OSError`).

## H13 — OAuth connector connection recursively acquires the configuration lock

Reproduced. The dispatch (`connect_authorized`, execution.py) holds
`service._configuration_lock` while `connect_and_discover()` runs; the OAuth preflight in
`MCPConnection._oauth_preflight` called `refresh_auth_cache(connector_id)` and
`ensure_token(...)` without a config, so both resolved configuration through
`OAuthCoordinator._resolve_config` → `service.refresh()` → the same non-reentrant lock.
A second call site is `OAuthBearerAuth.async_auth_flow` (transport handshake requests).

Fix:
- `OAuthCoordinator.ensure_token(..., config=None)`: when a validated immutable snapshot is
  provided, grant validation (before the POST, inside the refresh lock, and at commit) uses
  it instead of `service.refresh()`.
- `MCPConnection._oauth_preflight` passes its pinned validated `self.config` to
  `refresh_auth_cache(config=...)`, `ensure_token(config=...)`, and to the created
  `OAuthBearerAuth`.
- `OAuthBearerAuth` carries `config` and passes it on every `ensure_token` call.
- `execution._oauth_preflight` (call dispatch) also passes the dispatch config it already
  validated, removing the same lock inversion for concurrent connect/call races.

Files: `machina/connectors/oauth.py`, `machina/connectors/mcp_client.py`,
`machina/connectors/oauth_auth.py`, `machina/connectors/execution.py`.

Callouts (behavior): a live OAuth connection's bearer adapter now validates tokens against
the config it was approved with, instead of re-resolving durable config on every transport
request. Cross-process config changes still retire the connection through the local
service refresh in the calling paths (`execute_call`, `service.connect`, management flows);
the change removes a fail-closed check *inside* the approved dispatch window, where the
digest was already compared under the lock.

## H15 — stdio stderr drains can exhaust the shared thread executor

Reproduced. Each `_PipeStderr.start()` submitted a blocking `os.read` to the default
executor; with a 2-worker executor, 4 quiet drains starved an unrelated `asyncio.to_thread`
task (`TimeoutError`). Fix replaces the executor read with loop-level readiness:
`os.set_blocking(read_fd, False)` + `loop.add_reader(read_fd, ready.set)`; the drain task
waits on an `asyncio.Event`, reads one chunk per wake, and treats `BlockingIOError` as a
stale selector activation (macOS kqueue delivers one after a pipe drains — a blocking read
there would park the event loop). Where `add_reader` is unavailable (e.g. proactor
transports), the code restores blocking mode and keeps the previous bounded executor read.

Files: `machina/connectors/mcp_client.py` (`_PipeStderr`).

Callouts: `close()` now allows the reader task a second bounded moment to unwind (it
removes its loop reader) before closing the read fd. On platforms using the fallback, the
original per-connector executor exposure remains (documented in the class docstring).

## H16 — OAuth cancellation can be followed by credential publication

Reproduced deterministically. A controlled `_write_index` gate holds the store commit
inside `save`'s to_thread worker; `cancel()` returned `True` (state cancelled) and the
worker then published; the credential was loadable afterwards (RED assertion).

Fix (linearized under the store's index lock, per the review's suggested direction):
- `oauth_store.PublicationFence` + `OAuthCredentialStore.fence_publication(fence)`: sets the
  cancellation flag while holding the same index file lock the commit uses, so after it
  returns either the commit already published (`fence.published`) or it will refuse
  (`_commit` raises `OAuthPublicationCancelled` and the rollback deletes the fresh
  envelope).
- Every operation gets a fence at `start()`; `_run_operation` passes it to `save`.
- `cancel()` fences *before* `task.cancel()`, then retires a race-winning publication via
  `_retire_published` (store `forget` CAS on the published generation; newer authority
  wins).
- `_run_operation` re-checks for cancellation after `save` returns and retires the grant it
  just wrote instead of reporting `complete`.
- `forget()` / `invalidate_connector()` fence the in-flight publications they cancel before
  the retirement scan, closing the same race for logout/removal.

Files: `machina/connectors/oauth_store.py`, `machina/connectors/oauth.py`.

Callouts (behavior): `cancel()` now waits for the store index lock (bounded by
`OAUTH_LOCK_SECONDS`) and retires a grant that won the publication race; cancelling at the
last moment therefore ends signed out rather than leaving a live grant with a "cancelled"
status. Residual: if fencing itself times out (index lock held > 5s) *and* the flow task is
already cancelled, a raced publication can persist; the completion path reconciles whenever
the task observes the cancelled state. This requires pathological cross-process lock
contention on a local store.

## H17 — OAuth index publication failure can delete the credential it references

Reproduced. With the directory fsync failing after `os.replace` of `index.json`, the commit
raised, the blanket rollback deleted the new envelope, and the replaced index referenced a
missing file (`OSError` surfaced, credential lost).

Fix: in `save`'s `_commit`, a raising `_write_index` reconciles the visible index
(`_index_references_envelope`, read under the same held lock):
- referenced → the rename landed; publication committed (envelope bytes were fsynced before
  the rename) → keep the envelope, continue to GC/return the record;
- provably not referenced → safe rollback cleanup, original error re-raised;
- index unreadable → keep the envelope and re-raise (a leaked file is recoverable, an
  orphaned index entry loses the credential).

Files: `machina/connectors/oauth_store.py`.

Callouts (behavior): a post-rename directory-fsync failure on a visible publication is now
reported as success (`save` returns the record) instead of a storage failure; the index and
envelope stay consistent. The failed directory fsync affects crash-durability of that
rename, not its visibility.

## Tests

New file: `tests/test_connectors_holistic_fixes.py` (5 tests, DB-free determinism except
the DB-dependent suites below).

`logs/L3_green_focused.log` — 184 passed:
`test_connectors_holistic_fixes.py`, `test_connectors_client.py`,
`test_connectors_oauth_flow.py`, `test_connectors_oauth_store.py`,
`test_connectors_oauth_refresh.py`, `test_connectors_oauth_lifecycle.py`,
`test_connectors_oauth_hardening.py`, `test_connectors_oauth_execution.py`,
`test_connectors_oauth_service_regressions.py`, `test_connectors_oauth_deadline.py`,
`test_connectors_execution.py`, `test_connectors_management.py`,
`test_connectors_manager.py`, `test_final_review_connectors_fixes.py`.

Additional focused logs: `logs/L3_focused_oauth_a.log` (146 passed),
`logs/L3_focused_connectors_b.log` (173 passed),
`logs/L3_focused_integrations_c.log` (112 passed),
`logs/L3_focused_mcp_gateway_d.log` (55 passed): config/policy/DNS/network/callback,
fm3 lifecycle/generation, gateway connector OAuth, `mcp_manage_tool`, `oauth_cli_auth`,
`s5_oauth_stride`, `fm8_s5_broker`, `s5_guard_coverage`, `mcp_server`, gateway connectors.

Ruff: `logs/L3_ruff.log` — `ruff check` and `ruff format --check` clean over
`machina/connectors/` and the new test file.

Note: one combined run hit a cross-lane PostgreSQL artifact —
`asyncpg.exceptions.DeadlockDetectedError` in `permissions/store.py::create_approval`
(other lanes share `machina_test`); the test passed in isolation twice and the clean rerun
of the same 184-test set passed fully. Not related to these changes.

## Evidence paths

- `docs/verification/proofs/holistic_fixes_2026-10-15/L3_connectors.md` (this file)
- `docs/verification/proofs/holistic_fixes_2026-10-15/logs/L3_red_prefix.log`
- `docs/verification/proofs/holistic_fixes_2026-10-15/logs/L3_green_focused.log`
- `docs/verification/proofs/holistic_fixes_2026-10-15/logs/L3_focused_{oauth_a,connectors_b,integrations_c,mcp_gateway_d}.log`
- `docs/verification/proofs/holistic_fixes_2026-10-15/logs/L3_ruff.log`
