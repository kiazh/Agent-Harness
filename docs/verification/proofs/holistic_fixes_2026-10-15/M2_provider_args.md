# M2 — provider argument validation (H10) + /effort precedence (H23)

- Date: 2026-10-15 (EDT)
- Base commit: `3ef6d5b70744a9b12a740c46cfe4acbe6f418c27` (shared dirty worktree; other lanes edited other files concurrently)
- Lane files (ownership): `machina/core/config.py`, `machina/core/provider/transport.py`,
  `machina/gateway/features/providers_feature.py`, and their tests
  (`tests/test_provider_args_contract.py` new, `tests/test_r4_082_gateway_providers.py` extended)
- Source findings: `docs/verification/CODEBASE_REVIEW_HOLISTIC_2026-10-15.md` H10 (line 89), H23 (line 193)
- No commits, no pushes. Nothing outside the lane's files was modified.

## H10 — valid saved max_tokens values are rejected during execution

**Reproduced: YES.**

Pre-fix run (`logs/repro_h10.txt`, script `logs/repro_h10.py`):

```text
Config.set('max_tokens', 65536) -> ok  (cfg.max_tokens=65536)
save + reload -> max_tokens=65536 (saved successfully)
provider.complete(max_tokens=65536) -> ValidationError: max_tokens must be between 1 and 32768, got 65536
HTTP posts attempted: 0
RESULT: REPRODUCED — saved value is rejected at request time before provider contact
```

`validate_value("max_tokens", 65536)` / `Config.set` accepted anything ≥ 1 while every
provider transport (`_validate_params`, called by openrouter/ollama/direct/anthropic/chatgpt/copilot
before any request) rejected > 32768. Saving succeeded; the next call failed before provider contact.

**Root cause.** Two independent policies: config bound `(1, None)`, transport bound `1..32768`.

**Fix — one shared policy.**

- Authoritative ceiling source: the request-time check in
  `machina.core.provider.transport._validate_params` — it is the only enforcement every provider
  applies before sending a request, so its ceiling (`32768`) is the executable contract.
- The single literal now lives in `machina.core.config.MAX_TOKENS = 32768` (leaf module);
  transport imports it (`from machina.core.config import MAX_TOKENS, config`) and config uses it
  for `"max_tokens": (1, MAX_TOKENS)`. Rationale: transport already imports config, and config may
  never import the provider stack (`tests/test_config_import_cycle.py::test_config_load_pulls_no_provider_stack`);
  the dependency direction is therefore cycle-free, with one literal for both surfaces.
- "Provider-aware" in the finding is the per-provider **application** of the common wire contract:
  every provider runs `_validate_params` at request time against the shared ceiling. There is no
  per-provider output-limit data anywhere in the codebase (only input context windows in
  `machina/core/context_windows.py`), so inventing per-provider numbers was rejected as dishonest.
- Deliberate zero/unlimited semantics of other keys (`usage_*`, `rate_limit_*`, `eviction_max_tokens`)
  are untouched.
- `transport.py` line count kept exactly at its god-file ceiling (382; the constant name was chosen
  to keep the error line single-line and the change line-neutral — the provider package total is
  ratcheted at 5003).

**User-visible behavior change (callout).** Saving a value above the ceiling is now rejected at
write time with a clear typed error instead of being stored and breaking later turns:

| surface | before | after |
| --- | --- | --- |
| CLI `machina config-set max_tokens 65536` | stored, exit 0 | `max_tokens must be in range 1..32768, got 65536`, exit 1 |
| gateway `config.set` (`/config max_tokens 65536`) | stored | RpcError `INVALID_PARAMS`, same message |
| config.yaml already containing 65536 | loaded, every turn failed pre-provider | warning logged, default 4096 kept |
| per-call `max_tokens` ≤ 32768 | unchanged | unchanged |

The transport error text is unchanged (`max_tokens must be between 1 and 32768, got N`), now
interpolated from the shared constant.

**Tests (RED → GREEN).** `tests/test_provider_args_contract.py` (new, 6 tests). Pre-fix RED log:
`logs/red_h10.txt` (5 failed, "DID NOT RAISE" / boundary disagreement). Post-fix: all pass;
`logs/verify_h10.txt` shows the boundary/load/save invariants from a clean process.

## H23 — /effort reports a setting change that environment precedence can override

**Reproduced: YES.**

Pre-fix run (`logs/repro_h23.txt`, script `logs/repro_h23.py`):

```text
request /effort low -> response effort='low'
config.get('reasoning_effort') -> 'high' (env MACHINA_REASONING_EFFORT=high)
case1 shadowed-success: REPRODUCED
config.set('reasoning_effort','high') with env='' -> get()=''
RESULT: REPRODUCED — the echo claims the change while calls keep the env value
```

`models_set_effort` wrote the in-memory setting and echoed the requested effort, but
`Config.get` prefers `MACHINA_REASONING_EFFORT` — including an explicitly empty value, which is a
deliberate "provider default" signal for this key only.

**Fix — explicit runtime override with consistent precedence (the smaller honest option).**
The review offered either "expose effective setting + source" or "runtime override with consistent
precedence". The override was chosen because:

1. It is the established codebase pattern for exactly this class of bug:
   `sync_execution_mode_env` (`machina/gateway/features/mode.py`) mirrors a mode write into
   `MACHINA_EXECUTION_MODE` (+ `.env` when persistent); the CLI does the same for `model`/`provider`.
2. It fixes the user-visible defect without a UI change: `ui/src/features/settings.ts` `/effort`
   reads `result.effort` from `models.setEffort` and would keep claiming success under the
   "effective/source fields" variant unless the UI were changed (outside this lane's ownership).
3. The response schema is unchanged (`EffortSetResult` in `ui/src/protocol.ts`).

`_sync_reasoning_effort_env(effort, persist=...)` always writes the process environment; with
`persist=true` it also updates `.env` (in step with `config.set(..., persist=true)` writing
config.yaml), warning without failing if the `.env` write fails (in-process value already applies).
`off`/`none`/`default`/`auto` sync the empty value, preserving the documented explicit-empty
semantics. The echo (`effort` field) now always equals the effective `config.get(...)` value.

**User-visible behavior change (callout).** A `models.setEffort` call now updates
`MACHINA_REASONING_EFFORT` in the process environment (and in `.env` when `persist=true`), so a
previously set env var / `.env` line no longer shadows `/effort`; non-persisted calls still revert
after a restart, exactly as before. No wire/schema change.

**Tests (RED → GREEN).** `tests/test_r4_082_gateway_providers.py` +3 tests
(env shadow low, env shadow off, persisted `.env` sync). Pre-fix RED log: `logs/red_h23.txt`
(3 failed). Post-fix: pass; `logs/verify_h23.txt` verifies the same flow from a clean process.

## Focused suites (post-fix, all green)

Command prefix: `MACHINA_TEST_DATABASE_URL='postgresql://postgres:postgres@localhost:5432/machina_test' .venv/bin/python -m pytest -p no:cacheprovider ...`

- Config validation/precedence: `test_provider_args_contract.py`, `test_configuration_precedence.py`,
  `test_config_import_cycle.py`, `test_config_reentrancy.py`, `test_production_readiness_p1.py`,
  `test_code_review_fixes.py`, `test_r4_080_boot_config.py`, `test_gateway_config_atomicity.py`
  → **104 passed** (`logs/focused_config_transport.txt`)
- Transport/provider args: `test_provider_rate_limit.py`, `test_provider_context_audit.py`,
  `test_r5_011_013_provider_transport.py`, `test_deep_provider_fallback.py`, `test_providers_direct.py`,
  `test_provider_custom.py`, `test_fm2_provider.py` → **138 passed** (`logs/focused_provider_transport.txt`)
- Gateway providers feature: `test_gateway_providers.py`, `test_r4_082_gateway_providers.py`,
  `test_features_split.py`, `test_final_review_2026_10_15_providers.py`,
  `test_final_review_2026_10_15_gateway_mode.py` → **92 passed** (`logs/focused_gateway_providers.txt`)
- `tests/test_property_based.py -k params` → 5 passed (max_tokens property stays bounded at 32768)
- `transport.py` god-file ceiling (382) explicitly passes:
  `logs/transport_ceiling.txt` — `1 passed`.
- Ruff: `ruff check` + `ruff format --check` clean for all five lane files (whole-tree
  `ruff check machina/ tools/` also passes).

## Evidence index

`docs/verification/proofs/holistic_fixes_2026-10-15/logs/`

| file | content |
| --- | --- |
| `repro_h10.py` / `repro_h10.txt` | pre-fix H10 reproduction (save 65536 → transport rejection; 0 HTTP posts) |
| `red_h10.txt` | RED run: 5 failing regression tests before the fix |
| `verify_h10.py` / `verify_h10.txt` | post-fix boundary/load/save verification |
| `repro_h23.py` / `repro_h23.txt` | pre-fix H23 reproduction (echo 'low' while env keeps 'high') |
| `red_h23.txt` | RED run: 3 failing /effort tests before the fix |
| `verify_h23.py` / `verify_h23.txt` | post-fix effective-echo verification under env shadow |
| `focused_config_transport.txt` | 104 passed |
| `focused_provider_transport.txt` | 138 passed |
| `focused_gateway_providers.txt` | 92 passed |
| `transport_ceiling.txt` | transport.py 382-line ceiling test passes |
| `ratchet_state.txt` | package/copilot/transport line counts (cross-lane note below) |

Note: `repro_*.py` scripts archive the pre-fix behavior; re-running them against the fixed tree is
expected to fail fast inside the repro (the save now raises) — use `verify_*.py` for the post-fix run.

## Cross-lane notes (not fixed here — outside lane ownership)

`tests/test_architecture_layers.py` currently fails on three ratchets, none caused by this lane
(`logs/ratchet_state.txt`; my files contribute 0 deferred imports and 0 net lines in
`core/provider`):

1. `test_deferred_import_count_ratchet`: census 718 > cap 717. The only delta vs HEAD is
   `machina/services.py` +1 deferred import (verified per-file against HEAD, all M2 files delta 0).
   That lane or the coordinator must update the delta table/cap.
2. `test_god_file_ceilings_only_fall[core/provider/copilot.py-431]`: `copilot.py` is 443 lines
   (H12 lane's concurrent fix).
3. `test_provider_package_total_only_falls`: 5015 > 5003; the +12 is exactly the copilot.py growth
   above (transport.py held at exactly 382 by keeping the fix line-neutral).

Also, `ruff format --check machina/ tools/` reports `machina/connectors/oauth.py` unformatted
(connector lane, not touched here).
