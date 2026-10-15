# R4-080 / R4-082 / R4-083 — gateway + config lane (D1) evidence

Lane files: `machina/core/config.py`, `machina/core/provider/reasoning.py`,
`machina/cli/__init__.py`, `machina/db/connection.py`,
`machina/core/child_process.py`, `machina/gateway/server.py`,
`machina/gateway/features/providers_feature.py`, `machina/gateway/__main__.py`,
`ui/src/protocol.ts`, `ui/src/features/settings.ts` (+ owned tests).
`SECURITY.md` one-line touch for the R4-083 behavior note.
Follow-up addition: `ui/src/features/settings.ts` empty-key guard (+2 UI tests).

Artifacts in this dir:
`R4-080_082_083_red.txt` (RED on unfixed `efd94af` worktree, 16 failed),
`R4-080_082_083_green_focus.txt` (158 passed),
`R4-080_082_083_ruff.txt`, `R4-080_082_083_green_ui.txt`
(`npm run check` exit 0: 453 tests, 447 pass, 0 fail, 6 skipped),
`R4-082_settings_emptykey_red.txt` (RED for the follow-up guard, 3 fail /
132 pass — 1 genuine + 2 rename-pairing, see below).

DB: `machina_test` only (`MACHINA_TEST_DATABASE_URL`),
`.venv/bin/python`, `-p no:cacheprovider`. No commit.

## R4-080 boot/config — GREEN (4/4)

- (a) Non-mapping YAML root (`config.py`). RED: `AttributeError: 'list'
  object has no attribute 'items'` (and `'str'` variant) at `config.py:580`;
  the CLI E2E (`python -m machina config` under a list-root config) exited 1
  with a startup traceback instead of serving output. Fix: `isinstance(dict)`
  guard after load — warn naming path + actual type, keep defaults
  (`machina/core/config.py`). GREEN: list/scalar roots warn (`Ignoring
  config file … got list/str`) with defaults intact; CLI E2E exits 0, no
  traceback, real table output.
- (b) Recovery message (`cli/__init__.py:237`). RED asserted
  `machina db init` in output (no such command; the real one is
  `machina init`). Fix: one-word message change. GREEN: output is
  `Run machina init and retry.`, and `machina db init` no longer appears.
- (c) `Database.connect` race (`db/connection.py`). RED: 8 overlapping first
  connects built **8 pools** (`assert 8 == 1`). Fix: per-event-loop
  `asyncio.Lock` (documented: locks bind to the loop of first use, recreated
  per loop) + double-checked assignment; second pool can no longer leak.
  GREEN: exactly 1 `create_pool` call across 8 racers against real
  `machina_test`; surviving pool holds `get_size() == 1`, `SELECT 1` works.
  (An earlier delta-vs-`pg_stat_activity` assertion was dropped: parallel
  lanes share the same `machina_test` cluster, so absolute backend counts are
  not attributable — pool count + pool size is the exact leak signal.)
- (d) `run_bounded` NUL argv (`child_process.py`). RED: `ValueError: embedded
  null byte` escaped despite the "spawning never raises" contract. Fix:
  `except (OSError, ValueError)` → typed `BoundedResult(spawn_error=…)`,
  `returncode None`, `timed_out/truncated False`; docstring updated. GREEN.

## R4-082 gateway/providers — GREEN (3/3)

- (a) Effort vocabulary, single-sourced. Root cause: three disjoint
  definitions — `config.validate_value` (`''|low|medium|high|xhigh|max`),
  `provider.reasoning` (`low|medium|high` + its own unset list),
  catalog wire tiers (`none|minimal|low|medium|high|xhigh|max`) — so
  `models.setEffort(minimal)` validated, then `config.set` rejected it and
  the gateway escaped a `ValueError` as **INTERNAL_ERROR (-32603)** (RED
  frame in the log). Fix, cycle-safe: canonical tuples live in
  `core.config` (`REASONING_EFFORTS`, `REASONING_CATALOG_EFFORTS`,
  `REASONING_UNSET_EFFORTS`); `provider.reasoning` imports (not defines) the
  wire set and re-exports it, so `test_config_import_cycle.py` /
  `test_leaf_independence.py` stay green (config never imports provider).
  `config.set` additionally accepts `minimal` and normalizes
  `none|off|default|auto → ""`; both gateway surfaces (`config.set`,
  `models.setEffort`) route through the same vocabulary, and
  `models_set_effort` converts any residual drift into typed INVALID_PARAMS
  instead of INTERNAL_ERROR. `server.py`'s divergent inline
  `normalize_reasoning_effort` block deleted (now pure `coerce_config_value`
  + `config.set`). Non-catalog routes keep `low|medium|high`; rejections stay
  typed (`supported: low, medium, high`). GREEN: `minimal` persists end to end
  over real JSON-RPC frames; every catalog tier round-trips
  `models.setEffort → config.get`; `ultra`/garbage still rejected;
  `off` clears on both surfaces. `reasoning.py` stays at its 61-line ceiling
  (verified `wc -l`).
- (b) `providers.remove` fallback persists. RED: no `config.yaml` written,
  shadowing `MACHINA_PROVIDER/MACHINA_MODEL` env lines untouched — a restart
  resurrected the removed provider. Fix: new `config.persist_provider_fallback()`
  (config.yaml + in-place `.env`/env sync — the CLI's `_fall_back_from_removed_provider`
  mechanism factored into the shared helper without duplicating logic);
  gateway `providers_remove` calls it when the active provider is removed,
  warning (never raising) if persistence fails. GREEN: post-remove
  `config.yaml` carries `openrouter` + default model, `.env` lines updated in
  place, and a restart simulation (env cleared, fresh `Config.load`)
  resolves the fallback.
- (c) `apiKeyEnv` vs `envKey`. Decision: `apiKeyEnv` everywhere (matches the
  pre-existing `providers.list/add` payloads and `ProviderAddResult`).
  Server `providers_remove.cleanup` key renamed `envKey → apiKeyEnv`;
  `protocol.ts ProviderCleanup` + `settings.ts removalSummary` aligned; UI
  fixtures updated. Contract assertions on both sides: Python
  `set(cleanup) == {storeEntry, apiKeyEnv, envLineRemoved, envVarRemoved,
  cachedModels, registryEntry}` + `envKey` absent; TS `protocol-parity`
  test pins the exact backend key set and asserts `envKey` is gone.
  `grep envKey ui/src ui/test` → only the negative assertion. GREEN.

## R4-083 standalone token — GREEN

RED: `python -m machina.gateway` without `MACHINA_GATEWAY_TOKEN` minted a
token silently — stderr showed only the "accepts unauthenticated requests"
warning while every method except bare `initialize` answered unauthorized.
Fix (`gateway/__main__.py`): mint into the environment **before** Gateway
construction and print `gateway auth token: <64-hex> …` to stderr;
launcher-injected tokens are never re-printed; stale comment corrected;
`SECURITY.md` trust-boundary row notes the disclosure. GREEN (real
subprocess + real `machina_test`): token parsed from stderr, `initialize`
+ `providers.list` succeed with it, wrong token → UNAUTHORIZED, `shutdown`
clean, exit 0.

## Follow-up (1): settings.ts empty-key guard — GREEN

`addProvider`'s catch path ran `message.split(key).join("[redacted]")`; for a
keyless local endpoint `key === ""` and `"".split("")` shreds the message
(`c[redacted]o[redacted]u[redacted]l[redacted]d…`, full text in
`R4-082_settings_emptykey_red.txt`). Fix: reuse the empty-safe
`redactSecret` from `keys.ts` (no cycle — `keys.ts` never imports
`settings.ts`), 1 import + 1 call-site line. RED: keyless test fails with the
mangled text; keyed-redaction test passes pre- and post-fix (no-regression
guard). (The RED run also fails the two `/provider remove` tests — expected
pairing artifact: fixtures already carry the R4-082 `apiKeyEnv` rename while
`settings.ts` was stashed to pre-rename; they pass with the fix.) GREEN:
`features.test.ts` 135/135; full `npm run check` exit 0.

## Follow-up (2): secrets-allowlist gap — LEFT FOR R4-093 (decision + probe)

Live probe (no code change): `secrets.set` REFUSES `OPENCODE_API_KEY`,
`CUSTOM_API_KEY`, `CUSTOM_BASE_URL`, `KIMI_API_KEY` (INVALID_PARAMS
"not a managed secret key") while `OPENROUTER_API_KEY` is accepted — yet
`provider/specs.py:112` consumes `OPENCODE_API_KEY`, `factory.py:20-39`
consumes `CUSTOM_BASE_URL`/`CUSTOM_API_KEY`, and `config.SECRET_KEYS` /
`LEGACY_ENV_VARS` carry the opencode/kimi/custom keys. Consequence:
`OPENCODE_API_KEY`, `CUSTOM_API_KEY`, `KIMI_API_KEY` are unsettable from any
gateway/UI surface (`config.set` refuses secrets, `secrets.set` refuses the
names); only file/env setup works. (`CUSTOM_BASE_URL`/`OPENCODE_BASE_URL`
are fine — non-secret, settable via `config.set`.) The widening belongs in
`machina/security/env_file.py` (`PROVIDER_KEYS`) +
`machina/gateway/features/secrets.py` (allowlist) + `ui/src/features/keys.ts`
(`KNOWN_KEYS`) — all outside this lane (env-file lane owns the first; UI
scope here is `protocol.ts` + `settings.ts`). **Not changed; recommended for
the R4-093 CLI lane** with the probe above as the RED.

## Verification (final, current HEAD `948f61e` + lane diff)

- `tests/test_r4_080_boot_config.py` + `test_r4_082_gateway_providers.py` +
  `test_r4_083_gateway_standalone.py` + `test_config_import_cycle.py` +
  `test_leaf_independence.py` + `test_architecture_layers.py` +
  `test_gateway_providers.py` + `test_fm7_cli.py` → **158 passed**.
- Adjacent sweep (prior session): providers_direct, fm2_provider,
  model_catalog, chatgpt_provider, gateway, provider_store, mcp_server,
  terminal_safety, interop, production_readiness_p1, persona_description,
  config_reentrancy → 299 passed, 1 unrelated deadlock failure in
  `test_document_count_distinct_sources` that **passed on solo rerun**.
- `ruff format --check` + `ruff check` on all 11 touched py files → clean.
- `cd ui && npm run check` → exit 0 (typecheck + 453 tests: 447 pass,
  0 fail, 6 skipped + eslint).

## Surprises / residual risks

- Parallel lanes share one `machina_test` cluster: absolute
  `pg_stat_activity` counts are not attributable across lanes (see (c)).
- `DEFAULT_CONFIG_PATH` binds at import; subprocess E2E tests must pass an
  explicit path or isolated HOME (done).
- The R4-080(a) CLI RED on unfixed code surfaces as a startup-time import
  cascade (exit 1 + traceback), not the `.items()` AttributeError seen in the
  in-process tests — both prove "CLI cannot serve with a non-mapping root".
- `persist_provider_fallback` writes `MACHINA_PROVIDER/MACHINA_MODEL` into
  the gateway process env (required: `Config.get` checks env before the
  file); same behavior as the CLI fallback it mirrors.
- Other lanes have commits landing on this tree (`0547aea`); lane diff
  re-verified green after each.
