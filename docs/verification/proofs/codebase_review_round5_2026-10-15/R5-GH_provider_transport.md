# R5-GH — Provider transport correctness (findings 11, 12, 13)

Lane: R5-GH (subtasks G1 + G2 + G3). Date: 2026-10-15.
Plan: `docs/verification/FIX_PLAN_ROUND5_2026-10-15.md`.
Source review: `docs/verification/CODEBASE_REVIEW_CURRENT_2026-10-14.md` §11–13.

All three defects reproduced RED against unmodified provider code, then fixed
GREEN. Mocks exist only at the transport edge (`httpx.MockTransport`, stubbed
`client.post` / `client.stream`, `AsyncMock` bucket); the response cache, the
SSE parser, and the token buckets are the real seams.

## Files changed

| File | Change |
|---|---|
| `machina/core/provider/sse.py` | **NEW** — single shared SSE field parser (`sse_field` / `sse_data` / `sse_event`) |
| `machina/core/provider/ollama.py` | G1: endpoint-scoped cache key (`_cache_key_for`); G3: rate-limit binding |
| `machina/core/provider/direct.py` | G2: shared parser; G3: rate-limit binding |
| `machina/core/provider/anthropic.py` | G2: shared parser (`data:` + `event:`); G3: rate-limit binding |
| `machina/core/provider/chatgpt.py` | G2: shared parser; G3: rate-limit binding |
| `machina/core/provider/copilot.py` | G2: shared parser; G3: rate-limit binding |
| `machina/core/provider/openrouter.py` | G2: shared parser (both sites); G3: rate-limit binding |
| `machina/core/provider/rate_limit.py` | G3: `RateLimitBinding`, unconditional budget validation in `reload_rate_limiters` |
| `tests/test_r5_011_013_provider_transport.py` | **NEW** — RED-first proof suite (9 tests) |

Not touched: `machina/core/provider/audit.py` (R5-N lane).

## G1 — Ollama response cache does not identify the server (finding 11) — FIXED

RED (`test_r5_g1_ollama_cache_scoped_by_endpoint`): two providers with distinct
`base_url`s (`127.0.0.1:11434` / `:11435`), identical request within TTL →
second call returned `server-11434`, the **first** server's body; the second
endpoint was never contacted. Reason: the cache scope was
`f"ollama:{model}"` (no endpoint), while `direct.py` already namespaces by
endpoint + credential digest.

Fix: `OllamaProvider._cache_key_for` builds scope `f"ollama:{endpoint}:{model}"`
with `endpoint = base_url.rstrip("/")` (mirrors `DirectProvider`). `getattr`
keeps the key buildable for doubles constructed via `__new__`.

GREEN: `test_r5_g1_ollama_cache_scoped_by_endpoint` passes; positive control
`test_r5_g1_ollama_trailing_slash_shares_endpoint` (trailing-slash variants
still share one cache entry — 1 HTTP hit) passes both pre- and post-fix.

## G2 — Streaming parsers discard `data:` fields without a space (finding 12) — FIXED

RED (`test_r5_g2_sse_parser_tolerates_missing_space`,
`..._direct_stream_no_space_frames`, `..._anthropic_stream_no_space_frames`,
`..._openrouter_stream_no_space_frames`, `..._no_duplicated_sse_prefixes`):
streams using `data:{...}` / `data:[DONE]` were dropped; the direct and
openrouter streams raised `ProviderError: ... stream ended without a terminal
frame ...`, and the anthropic stream lost both `event:` and `data:` lines.
Reason: every site required the literal prefix `"data: "` (and anthropic
`"event: "`) and sliced a fixed offset.

Fix: one shared parser in `machina/core/provider/sse.py`. `sse_field(line,
field)` strips at most one optional space after the colon (per the SSE spec),
treats a bare field name as empty, and returns `None` for non-matching lines.
All five SSE sites route through `sse_data` (anthropic also `sse_event`); the
duplicated `startswith("data: ")` / `line[6:]` logic is deleted. `[DONE]`
sentinels compare on `.strip()`. Ollama is intentionally excluded — it streams
NDJSON, not SSE.

GREEN: all G2 tests pass, including behavioral coverage for the
OpenAI-compatible dialect (direct), the Anthropic event/data dialect, and
OpenRouter (paid model id, so the free-tier fallback chain stays out of the
path). The structural test pins that all six files call the shared parser and
that no narrow `"data: "` literal remains.

## G3 — Rate-limiter config does not update live providers (finding 13) — FIXED

RED (`test_r5_g3_live_provider_tracks_reload`): construct a provider at 60
calls/min, change config to 600, call `reload_rate_limiters()` → the provider's
`_rate_limiter` was still the construction-time object (rate 1.0), while the
registry held a new one (rate 10.0). Reason: providers captured the bucket
**object** at construction; `reload_rate_limiters` swapped registry **entries**,
so live providers kept the stale bucket and never followed a runtime change.

Fix: `RateLimitBinding` (in `rate_limit.py`). Providers now store only
`_rate_limit_key`; `self._rate_limiter` is a property resolving
`_get_rate_limiter(key)` on every access, so a reload reaches live providers.
Assignment (`provider._rate_limiter = ...`) goes through a setter that installs
an instance override honored verbatim — the bypass seam the provider test
suite uses to disable throttling — and the override never leaks into the shared
registry. Replacement (not in-place mutation) is deliberate: a reader holding
the old object finishes its single `acquire` against a consistent snapshot
rather than racing a mid-acquire parameter rewrite.

Behavior/API changes called out:
- A provider no longer materializes its bucket at construction; the bucket is
  created on first `_rate_limiter` access (first request). The registry stays
  lazily populated, matching `reload_rate_limiters`'s "only replace existing
  buckets" contract.
- `reload_rate_limiters()` now validates the configured budget unconditionally
  (via `_build_rate_limiter`) before the replace loop. Previously, with an empty
  registry, an invalid `rate_limit_calls_per_minute` was a silent no-op. This
  surfaced via the pre-existing order-dependent test
  `test_fm2_provider.py::test_reload_rate_limiters_rejects_invalid_config`
  (which relies on a populated registry); the guard makes it correct in
  isolation and is strictly safer.

GREEN: `test_r5_g3_live_provider_tracks_reload` passes; positive control
`test_r5_g3_instance_override_still_honored` (assignment bypass keeps working)
passes pre- and post-fix. The existing
`test_fm2_provider.py::test_reload_rate_limiters_rebuilds_existing_buckets`
(asserts the registry entry is *replaced*, `rebuilt is not bucket`) still passes.

## Verification

- Proof suite: `tests/test_r5_011_013_provider_transport.py` — RED
  `7 failed, 2 passed` (the 2 are the positive controls) → GREEN `9 passed`.
- Regression sweep (providers, usage, cache, audit, chaos, property, adversarial
  auth, permissions): **379 passed, 1 skipped**.
- `ruff check` clean; `ruff format` clean on every changed file.

Logs (`docs/verification/proofs/codebase_review_round5_2026-10-15/logs/`):
- `R5-GH_red_unfixed.log` — my suite against unmodified HEAD provider code.
- `R5-GH_green.log` — my suite against the fixed tree.
- `R5-GH_regression.log` — 379 passed / 1 skipped.

### Reproduce

```sh
set -o pipefail
.venv/bin/python -m pytest tests/test_r5_011_013_provider_transport.py -q
# RED evidence: run the same suite against a clean HEAD checkout with
# PYTHONPATH pointing at it (provider code unmodified).
.venv/bin/python -m pytest \
  tests/test_providers_direct.py tests/test_fm2_provider.py \
  tests/test_provider_usage.py tests/test_provider_custom.py \
  tests/test_chatgpt_provider.py tests/test_copilot_provider.py \
  tests/test_anthropic_tool_results.py tests/test_kimi_provider.py \
  tests/test_deep_provider_fallback.py tests/test_provider_context_audit.py \
  tests/test_e1_prompt_cache.py tests/test_audit_regressions.py \
  tests/test_chaos_extended.py tests/test_property_based.py \
  tests/test_chatgpt_auth_adversarial.py tests/test_integration_permissions.py -q
```

## Out of lane scope — architecture ratchet re-baseline (coordinator action)

`tests/test_architecture_layers.py` (`GOD_FILE_CEILINGS` /
`_PROVIDER_PACKAGE_TOTAL_CEILING`) is a shared ratchet. It is **already red at
committed HEAD**, independent of this lane: `core/agent.py` (2057),
`core/provider/audit.py` (147, R5-N), and the package total (4852 > 4780). This
lane adds the new `sse.py` file the plan explicitly authorizes, so the ratchet
must be re-baselined once all lanes land. Deltas attributable to R5-GH:

| File | HEAD | now | Δ |
|---|---|---|---|
| `core/provider/ollama.py` | 378 | 388 | +10 |
| `core/provider/direct.py` | 347 | 348 | +1 |
| `core/provider/anthropic.py` | 577 | 580 | +3 |
| `core/provider/chatgpt.py` | 678 | 680 | +2 |
| `core/provider/copilot.py` | 425 | 427 | +2 |
| `core/provider/openrouter.py` | 607 | 608 | +1 |
| `core/provider/rate_limit.py` | 79 | 120 | +41 |
| `core/provider/sse.py` | — | 46 | +46 |
| package total | 4852 | 4958 | +106 |

I did not edit `tests/test_architecture_layers.py`: it is outside this lane's
sole-writer set and its agent.py / audit.py entries belong to other lanes'
growth, so a single-lane bump would half-update an interdependent ratchet.

Unrelated pre-existing red (present at committed HEAD, not caused by this lane):
`tests/test_connectors_execution.py::test_native_mcp_tools_are_lazy_and_uncached`
fails at import time (`test_connectors_execution.py:16: ImportError`).

## Verdicts

- G1 (finding 11): **FIXED** — endpoint in Ollama cache identity.
- G2 (finding 12): **FIXED** — one shared, space-tolerant SSE parser; duplicated
  prefixes deleted across all six providers.
- G3 (finding 13): **FIXED** — live providers resolve the registry bucket per
  access; `reload_rate_limiters` reaches constructed providers and validates the
  budget unconditionally.
