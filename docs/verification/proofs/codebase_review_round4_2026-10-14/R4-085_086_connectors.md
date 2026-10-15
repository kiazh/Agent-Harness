# R4-085 / R4-086 — OAuth connector cluster + pattern-screener relaxation

Lane: connectors (`machina/connectors/oauth.py`, `models.py`, `oauth_callback.py`,
`policy.py` + tests). No commit (per lane instructions).

Proof matrix: adversarial (OAuth fixtures, loopback callback, race harness,
pattern corpus). RED run on unfixed code first, then GREEN after.

## Files changed

Source (4):

- `machina/connectors/oauth.py` — (a) removed all 5 `or {}` dead guards so
  `OAuthSettings.from_dict(None)` returns `None` and the `oauth_not_configured`
  refusal is reachable; (b) `preview()` catches `ValueError` from discovery and
  re-raises a typed `OAuthError(oauth_discovery_failed)` (defence in depth for
  in-memory configs that bypassed `validate()`); (c) `refresh_auth_cache()`
  coalesces on a live refresh via `_await_in_flight_refresh()` (bounded wait on
  the store refresh lock + one re-read) with a per-key `_refresh_owners`
  task-identity guard so the lock holder never waits on itself
  (`_owned_refresh_lock`); crash journals and timed-out waits stay fail-closed
  (signed-out); (e) `_public_auth()` / `_primary_binding()` index reads moved
  to `asyncio.to_thread`.
- `machina/connectors/models.py` — (b) `ConnectorConfig.validate()` reads
  `parts.port` eagerly inside try/except `ValueError`, so `:99999`, `:abc`,
  `:-1`, and malformed netlocs (broken IPv6) are typed `ConfigError` at save
  time; `store.configure()` never persists them.
- `machina/connectors/oauth_callback.py` — (d) ASCII-check on `state` (both
  operands) before `hmac.compare_digest`; non-ASCII `state` returns the normal
  error page, no `TypeError` escapes the handler.
- `machina/connectors/policy.py` — (R4-086) `_screen_pattern_budget()` rewritten
  from "reject ANY nested counted repetition" to a product budget
  (`_NESTED_REPETITION_BUDGET = 1000`): per-group expansion is tracked,
  `{n,}`-inside-a-counted-group is refused (unbounded), single counts keep the
  10k cap. Existing rejection tests re-baselined deliberately.

Tests (4):

- `tests/test_connectors_oauth_hardening.py` (new) — bullets (a), (b), (e).
- `tests/test_connectors_oauth_refresh.py` — bullet (c): 5-round coalescing
  proof + bounded-wait/fail-closed proof.
- `tests/test_connectors_oauth_callback.py` — bullet (d): non-ASCII `state`
  error page + live-flow-not-consumed.
- `tests/test_connectors_policy.py` — R4-086: IPv4 accept case, budget
  boundaries (1000 accept / 1100 reject), unbounded-outer refusal,
  re-baselined rejection list.

## Per-bullet RED / GREEN

RED run: new tests copied onto a clean worktree at `efd94af` (unfixed
`sources`, `PYTHONPATH` pointed at the worktree): **17 failed, 42 passed**.
GREEN run on this tree: **74 passed**
(`R4-085_086_green_pytest.txt`); required suites **107 passed** + full
connector glob **267 passed** (`R4-085_086_green_required_suites.txt`,
`MACHINA_TEST_DATABASE_URL` set, `-p no:cacheprovider`).

- (a) dead `oauth_not_configured` guard — RED: `test_preview_refuses_...`
  failed with "oauth discovery must not run…" (preview proceeded to discovery
  instead of refusing); `test_removed_oauth_settings_...` failed
  (`auth_identity` still returned the stale grant). GREEN: preview raises
  `oauth_not_configured` with zero discovery traffic; `status()` reports
  `not_configured`; cache drops the grant.
- (b) invalid ports — RED: 4 `..._rejected_at_save` params `DID NOT RAISE
  ConfigError`; unvalidated-endpoint preview raised raw `ValueError: Port out
  of range` / `Invalid IPv6 URL`. GREEN: typed `ConfigError` at
  `validate_input`/`configure` (nothing persisted); preview raises typed
  `OAuthError(oauth_discovery_failed)`, never raw `ValueError`.
- (c) in-flight preflight hard-fail — RED: `test_in_flight_refresh_preflight_
  coalesces_...` failed ("preflight must coalesce on the live refresh" — the
  preflight completed immediately with signed-out while the refresh was live),
  5/5 rounds would fail the same way (single-round harness proves the
  mechanism; the GREEN test repeats it 5/5). GREEN: preflight stays pending
  until the refresh commits, then returns the winner's identity (same
  grantId/generation); lock semantics preserved
  (`test_overlapping_refresh_waits_for_existing_journal_owner` still green);
  crash journal still forces reauthorization
  (`test_crash_after_in_flight_forces_reauthorization` green); bounded-wait
  test proves <1s fail-closed signed-out when the refresh outlasts the bound.
  Self-deadlock guard verified by the full refresh suite (every `ensure_token`
  refresh path calls `refresh_auth_cache` under the owned lock).
- (d) non-ASCII `state` — RED: `TypeError` escaped `_handle` (connection
  reset, empty response, loop exception handler captured `TypeError`).
  GREEN: normal error page bytes, no loop exception, legitimate flow
  unconsumed and completable afterwards.
- (e) sync index reads on the loop — RED: both call sites ran on the main
  thread and a 0.2s read stalled the loop ticker. GREEN: both reads run on
  worker threads, ticker keeps firing (≥10 ticks during reads).
- R4-086 screener relaxation — RED: IPv4 pattern + `^(a{2}){3}$` +
  `^((a{2}){3}){5}$` + `^(a{100}){10}$` all rejected with "nested counted
  repetitions are not supported". GREEN: all accepted and match/reject
  correctly; `^(a{100}){11}$` (product 1100), `(a{10000}){10000}`,
  `(?:a{20000}){20000}`, `(a{50000}){50000}?`, `a{100000000}`, and unbounded
  outer `(?:[0-9]{1,3}){3,}` all still rejected with typed `ConnectorToolError`
  in <1s (no compile freeze). Fail-closed direction preserved: only products
  ≤1000 with a bounded outer count are admitted; anything unbounded or
  over-budget fails closed before `regex.compile`.

## Commands

```bash
export MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test
.venv/bin/python -m pytest tests/test_connectors_oauth_hardening.py \
  tests/test_connectors_oauth_callback.py tests/test_connectors_policy.py \
  "tests/test_connectors_oauth_refresh.py::test_in_flight_refresh_preflight_coalesces_for_concurrent_callers" \
  "tests/test_connectors_oauth_refresh.py::test_in_flight_preflight_wait_is_bounded_and_fails_closed" \
  tests/test_connectors_oauth_refresh.py -q -p no:cacheprovider
.venv/bin/python -m pytest tests/test_connectors_policy.py \
  tests/test_connectors_oauth_flow.py tests/test_connectors_oauth_network.py \
  tests/test_connectors_oauth_lifecycle.py tests/test_gateway_connector_oauth.py \
  tests/test_architecture_layers.py -q -p no:cacheprovider
# + every other tests/test_connectors_*.py, test_gateway_connector_oauth*.py,
#   test_fm8_s5_broker.py, test_s5_guard_coverage.py (see green log)
.venv/bin/ruff format --check <lane files>; .venv/bin/ruff check <lane files>
```

`ruff format --check` + `ruff check`: clean on all 8 lane files.

## Residual risks / notes

- During verification, heavy DB-touching connector tests (`test_connectors_
  execution.py`, `test_gateway_connector_oauth.py`, `test_s5_guard_coverage.py`)
  flaked under cross-session load (load avg >20, 11+ concurrent pytest
  processes): `asyncpg DeadlockDetectedError` in `session_manager.delete` /
  `store.create_approval` teardown, and once a deadlock-error string inside a
  stdio echo tool result. All are shared-`machina_test`-DB contention between
  sibling sessions — none touch lane code paths (verified: failing asserts
  are in session/teardown code; final low-load gate is 107 + 267 fully green;
  the flaky param case passes 3/3 standalone).
- `_refresh_owners` is keyed per coordinator instance; cross-process refresh
  races still serialize on the file lock as before (unchanged semantics).
- The screener budget (1000) is a heuristic: worst-case admitted compile is
  small (bounded product of ≤10k single counts), and matching stays under the
  existing per-match timeout + per-call budget. Genuinely explosive nested
  quantifiers without counts (`(a+)+$`) remain covered by the `regex` engine
  timeout tests, untouched.
- `oauth_network.py:501` still contains one `getattr(config, "oauth", None) or {}`
  call site, but `oauth_network.py` is outside this lane so it was left
  untouched. Both production callers of `OAuthNetworkClient.discover`
  (`oauth.py:177` in `preview`, `oauth.py:406` in `_run_operation`) are now
  covered by the reachable coordinator guard: `preview` refuses before any
  discovery traffic (proven by the forbidden-network test), and
  `_run_operation` refuses at `oauth.py:419-421` (operation fails closed, no
  approval gate reached, nothing persisted). Residual nuance: if oauth
  settings are removed between `start()` and the revalidation rediscover,
  one bounded discovery probe runs under default auto settings before the
  guard refuses — wasted traffic only, still fail-closed. Fixing the
  network-level fallback belongs to the `oauth_network.py` owner.
