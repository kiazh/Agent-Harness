# Final-review connector fixes (F03–F06 + screener F1)

- Date: 2026-10-15
- Lane: connectors (sole writer). Baseline followed
  `docs/verification/CODEBASE_REVIEW_FOLLOWUP_2026-10-15.md` (F03–F06) and
  `docs/verification/proofs/final_review_2026-10-15/line_memory_connectors.md`
  (F1). No commits.
- Method: RED-first deterministic probes against the original sources, then
  smallest safe change, typed errors, focused suites + ruff.
- Logs: `docs/verification/proofs/final_review_2026-10-15/logs/`.

## Files changed

| File | Finding(s) |
| --- | --- |
| `machina/connectors/management.py` | F03 (revision CAS), F04 (pre-write refusal) |
| `machina/connectors/service.py` | F04 (common launch boundary), F05 (off-loop store write), F06 (lock order) |
| `machina/connectors/store.py` | F05 (bounded nonblocking lock + typed busy + async wrappers) |
| `machina/connectors/policy.py` | F1 (body-size x count node budget) |
| `tests/test_final_review_connectors_fixes.py` | new evidence tests (all findings) |

No other file was touched.

## RED evidence (original sources)

`logs/red_probe_original_sources.txt` — all five findings reproduce:

```
  F1: REPRODUCED — literal-body-180: admitted, compile=0.006s; alternation-body: admitted, compile=0.241s
  F3: REPRODUCED — newer config clobbered -> ['--original'] (store=previous configuration restored)
  F4: REPRODUCED — sandbox stdio launch path reached without a typed refusal
  F5: REPRODUCED — ConnectorStoreBusy=False nonblocking_flock=False
  F6: REPRODUCED — config lock held during auth refresh: seen=[True]
```

F1 characterisation: the literal-body case is cheap in this engine (it
optimises repeated literals), but an admitted alternation-heavy body under a
large count compiles synchronously in ~0.24 s (and the class scales;
`(a{100}){10000}` compiles ~0.12 s and `(a{10000}){10000}` freezes — those are
already refused by the existing nested-count screen). The hole is that an
*unquantified* body is never charged at all, so admit-cost is not bounded by
the screen. See `logs/red_probe_original_sources.txt`.

## Per-finding

### F03 — failed-add compensation is a revision CAS

`management._compensate` ran under a worker-local configuration lock and
unconditionally removed/restored config after the connection await. A newer
configuration persisted by another process during that await was clobbered.

Fix: `add_and_connect` captures the revision `_configure_locked` wrote;
`_compensate` reloads durable config first, then only removes/restores when the
current revision still equals the one it wrote. Otherwise it returns
`"compensation skipped: configuration changed during connect"`. A concurrent
removal (`current is None`) is also preserved.

- Tests: `test_failed_replace_keeps_newer_concurrent_config`,
  `test_failed_new_add_keeps_newer_concurrent_config`,
  `test_failed_add_without_interference_still_compensates` (uncontended path
  still yields `"configuration removed"`).

### F04 — sandbox stdio refused at the common launch boundary

Agent `mcp_connect` reached `mcp_client._connect_stdio` (host launch) without
the sandbox-transport rule the connect/invoke paths enforce.

Fix: one authoritative `service.refuse_sandbox_stdio(config, mode)` invoked
(a) in `management.add_and_connect` immediately after approval and *before any
store write*, and (b) inside `service.connect_and_discover(..., mode=...)`, the
primitive shared by the human and agent launch paths. Typed `ConnectorToolError`
("host stdio MCP is unavailable in sandbox mode"). HTTP is unaffected.

- Tests: `test_agent_mcp_connect_refuses_stdio_in_sandbox` (asserts no store
  file written), `test_connect_and_discover_refuses_stdio_in_sandbox`,
  `test_connect_and_discover_allows_http_in_sandbox`.

### F05 — bounded, nonblocking, off-loop store lock

`ConnectorStore._locked` used blocking `flock(LOCK_EX)` with no deadline,
called synchronously from `service._configure_locked`/`_remove_locked`, so a
stalled holder froze the event loop.

Fix: POSIX acquisition is `flock(LOCK_EX | LOCK_NB)` polled to a deadline
(`STORE_LOCK_TIMEOUT_SECONDS = 10.0`, injectable via
`ConnectorStore(lock_timeout=...)`); expiry raises the typed
`ConnectorStoreBusy(ConfigError)` naming only the lock file. Windows keeps its
bounded `LK_LOCK` idiom. New `configure_async`/`remove_async` run the whole
read-modify-replace in `asyncio.to_thread`, and the service uses them.

- Tests: `test_store_flock_uses_nonblocking_acquisition` (flags spy),
  `test_async_store_write_stays_off_the_event_loop` (heartbeat keeps ticking
  while the lock is awaited), `test_async_store_write_times_out_typed_when_lock_is_held`.

### F06 — one lock order: config → (outside it) credential

`service.refresh()` (and `_configure_locked`/`_remove_locked`) refreshed the
OAuth cache while holding the configuration lock; `oauth.refresh_auth_cache`
can wait on the credential refresh lock, whose owner re-resolves config through
`service.refresh()` — a circular wait until the coalescing bound, then the
cache was cleared.

Fix: the configuration lock is released before any `_refresh_auth_cache()`
call (in `refresh`, and moved out of `_configure_locked`/`_remove_locked` into
the public `configure`/`remove` after the lock). No path waits on a credential
lock while holding the configuration lock. `invalidate_connector` inside
`_refresh_locked` still pops the affected cache, so removal replacement keeps
correct auth views.

- Test: `test_config_lock_not_held_during_auth_cache_refresh` asserts the
  configuration lock is free each time `refresh_auth_cache` is invoked, across
  `refresh`, `configure`, and `remove`.

### F1 — screener charges repeated-unit size, not only nested counts

`_screen_pattern_budget` only charged counted quantifiers and products of
nested counts; an unquantified grouped body was never charged, so
`(<long body>){10000}` was admitted.

Fix: new `_screen_pattern_node_budget` (called from `_check_schema` after the
count screen) parses groups/classes/escapes and charges each counted unit its
size x count against `_MAX_PATTERN_NODES = 50_000`; unbounded `{n,}` forms are
loops and charge one copy. The count screen and its nested-product budget are
unchanged, so existing accept/reject cases hold.

- Tests: `test_grouped_long_body_under_a_count_is_refused` (literal and
  alternation bodies; matches the budget message, so a later match failure
  cannot mask the admit hole), `test_long_body_small_count_and_atom_counts_stay_accepted`
  (IPv4, host mask, small grouped body, exactly-at-budget nested product).

## Verification

| Command | Result |
| --- | --- |
| `pytest tests/test_final_review_connectors_fixes.py` | 13 passed |
| focused connectors oauth/execution/policy/refresh/callback/management + mcp manage + architecture pairs | 175 passed, 6 skipped |
| all `tests/test_connectors_*.py` + `test_mcp_manage_tool.py` + new tests | 363 passed, 6 skipped |
| `ruff check` + `ruff format --check` (changed files) | clean / formatted |

Logs: `logs/new_evidence_tests_verbose.txt`, `logs/connectors_focused_suites.txt`,
`logs/connectors_all.txt`, `logs/connectors_fix_ruff.txt`,
`logs/red_probe_original_sources.txt`.

### Notes / residual

- One run of the full connector set showed a single flaky failure in
  `test_connectors_execution.py::test_sdk_remote_call_runs_through_agent_approval_pipeline[boom-…]`;
  it passes in isolation and the full set is green on re-run (381 passed, 6
  skipped). Not reproducible and unrelated to these diffs.
- `test_architecture_layers.py` provider-package ceiling failures are caused by
  another lane's `core/provider/*` edits, not this lane (no connector layer
  test failed).
- Residual (pre-existing, not regressed): `_refresh_locked` may call
  `oauth.invalidate_connector` → credential-store file lock while holding the
  configuration lock when a config generation changed during a live refresh;
  that file lock is bounded by `OAUTH_LOCK_SECONDS` and now cannot circular-wait
  with the credential *refresh* lock.
