# L5 — gateway orchestration fixes (H20, H21)

Lane L5 of the holistic-review fix wave. Source of truth:
`docs/verification/CODEBASE_REVIEW_HOLISTIC_2026-10-15.md` findings **H20**
(P1) and **H21** (P2). Snapshot: `3ef6d5b70744a9b12a740c46cfe4acbe6f418c27`
(working tree also carries other lanes' concurrent changes; none of their
files were touched by this lane).

## Files owned and changed

| File | Change |
| --- | --- |
| `machina/services.py` | `_effective_session_route()`; compression builds its summarization provider from the session's effective route |
| `machina/gateway/features/sessions.py` | `context.compress` no longer passes the gateway's global defaults as explicit overrides |
| `machina/gateway/__main__.py` | entry-point fd juggling runs only under `__main__`; stdio loop dispatches tracked chain tasks + preemptive control requests with a bounded drain |
| `tests/test_holistic_l5_gateway_2026_10_15.py` | new RED-first regression suite (5 tests) |
| `tests/test_gateway_connectors.py` | one ordering assertion adapted to id-correlation (H21 callout; see below) |

Frozen hashes (sha256):

```
57aca472966266e5b783d5e73f304ae9c751a09b58107f8b47b0adb4d667eeaa  machina/gateway/__main__.py
e58bf2a60751e3d2dd342045a913b2469559df3dacb9c91a14e556a235a7cdd2  machina/gateway/features/sessions.py
2334d1c743b9a922407b1fe702e8bf5ad71552cb761bc1e02a0c8e6535b473fd  machina/services.py
4abbfbd706fdfe71f3f0faf07b4edb6789c5f1152d8f424512f5ef3b8b671bef  tests/test_holistic_l5_gateway_2026_10_15.py
8591ddcbdd70240dac0cbc1bfcedbf5bf1260959779713d1afadda16b8620a6b  tests/test_gateway_connectors.py
```

---

## H20 (P1) — explicit compression can send a session to the wrong provider

**Reproduced: YES.** Both surfaces the finding names independently construct
the summarization provider from gateway/global defaults instead of the
session's pinned route:

* `gateway/features/sessions.py` passed `model=gw.model, provider=gw.provider`
  (the gateway's globals) as explicit overrides.
* `services.py` fell back to `config.get("provider")/config.get("model")` when
  no override was present — the CLI `machina compress` path and
  `maybe_auto_compact` (auto-compaction inside a live turn) both hit this.

RED evidence (`logs/l5_h20_red.txt`, pre-fix, final tests):

```
AssertionError: assert [('openrouter...router/free')] == [('ollama', 'llama3.1')]
```

A session pinned to `provider="ollama", model="llama3.1"` was summarized
through `("openrouter", "openrouter/free")` — the gateway/global default — in
both the explicit `context.compress` RPC test and the services/CLI test.

**Fix.**

1. New `services._effective_session_route(session, *, provider=None,
   model=None)` mirrors the precedence of ordinary turns
   (`build_agent_for_session`, AH-021/AH-022): explicit override → session pin
   → agent definition → global config. The definition registry is consulted
   only when the session pin leaves a route part unresolved (no needless
   `agents` query on the common pinned path); a lookup failure degrades to the
   session/global route instead of failing compression (compression is
   auxiliary, and a provider-construction failure already falls back to
   truncation — never to a different provider).
2. `compress_session_with_status` builds `get_provider(provider=provider_name,
   model=model_name)` from that resolver. Overrides remain first-class for
   callers that intentionally request them.
3. `context_compress` no longer passes `gw.model`/`gw.provider`; the operation
   has no explicit per-request route knobs, so it applies no override.

All compression entry points now share the resolver: `context.compress`, CLI
`machina compress`, and auto-compaction (`turn_engine` →
`maybe_auto_compact` → `compress_session`).

**Tests (GREEN `logs/l5_h20_green.txt`)**

* `test_explicit_compression_uses_pinned_session_route` — real DB session
  pinned to Ollama, fake gateway with OpenRouter defaults, LLM summarization
  enabled, fake `get_provider` capture: asserts exactly
  `[("ollama", "llama3.1")]` and owned-provider close.
* `test_compress_services_route_matches_agent_factory_precedence` — no
  overrides: session pin wins; with no pin, the agent definition route is used
  (definition-only fallback pinned explicitly).

**Callouts.** Privacy-preserving fallback change: when a session's pinned
provider cannot be constructed (no key / unreachable route), compression now
falls back to truncation instead of silently switching to the global provider.
That is the intended behavior per the finding.

---

## H21 (P2) — long management requests block cancellation/approval/revocation RPCs

**Reproduced: YES.** Pre-fix `_serve` awaited `handle_line()` before reading
the next queued line. RED evidence (`logs/l5_h21_red.txt`, pre-fix sources +
final tests):

```
AssertionError: assert {'approvals.resolve', 'mode.revoke',
                        'prompt.cancel', 'shutdown'} <= {'interop.run'}
```

After a 2 s bounded wait with the slow `interop.run` still in flight, only
`interop.run` had been dispatched. The EOF test timed out the same way: the
serial loop never returned to read the drain.

**Fix (`machina/gateway/__main__.py`).**

* `_dispatch_requests(gateway, queue)` replaces the awaited loop:
  * **ordinary requests** are chained tracked tasks (`create_task`; each waits
    for its predecessor via `asyncio.wait({previous})`), preserving strict
    arrival-order execution for shared-state mutations — the same serial
    semantics as the former loop;
  * **control requests** (`prompt.cancel`, `approvals.resolve`,
    `mode.revoke`, `shutdown`) are awaited immediately instead of behind the
    chain, so slow harness/delegation/discovery work cannot delay them;
  * on exit (shutdown or EOF) every request already read is given
    `REQUEST_DRAIN_TIMEOUT_SECONDS = 10.0` to finish and answer; a handler
    that outlasts the drain is cancelled and joined (no dangling tasks, no
    process held open); `Gateway.close()` still owns turn/resource teardown;
  * handler exceptions are retrieved and logged (the loop used to propagate
    them out of `_serve`; `handle_line` is documented to answer its own
    RpcErrors, so this is a bug path).
* Entry-point fd juggling now runs only when the module is executed as
  `__main__`; importing the module (tests) leaves the importer's streams
  untouched. Production invocation (`python -m machina.gateway` and
  `runpy.run_module(..., run_name="__main__")`) is unchanged — verified by the
  subprocess suites below.

**Tests (GREEN `logs/l5_h21_green.txt`, final `logs/l5_h20_h21_green_final.txt`)**

* `test_control_requests_dispatch_during_slow_request` — slow `interop.run`
  plus all four control requests pipelined behind it: every control request
  dispatches (in relative order) while the slow request is still blocked.
* `test_pipelined_requests_drain_before_shutdown_closes` — orderly shutdown:
  requests already read are not cancelled, their responses are written.
* `test_eof_cancels_requests_that_outlast_the_drain` — drain bound
  (patched to 0.05 s) cancels and joins a wedged request instead of hanging.

**Behavior-change callouts (explicit, per lane instructions).**

1. **Concurrency model**: the stdio loop is no longer strictly
   one-request-at-a-time for everything. Ordinary requests keep FIFO execution;
   control requests preempt pending chain work. This is the point of H21.
2. **Response ordering**: JSON-RPC responses are correlated by `id` (both UI
   `gateway.ts` and the protocol require nothing more), so a control request
   may answer before earlier pipelined chain requests. Consequently one
   pre-existing wire test that asserted positional frame order —
   `tests/test_gateway_connectors.py::test_actual_stdio_gateway_reports_empty_
   harness_inventory_without_initializing` — was adapted to id-keyed
   assertions (all three responses still asserted, none weakened). This is the
   only file touched outside the owned set; it is the stdio entry-point wire
   test that the change necessarily affects.
3. **Control preemption vs. state establishment**: a control request
   pipelined in the same burst *behind a state-establishing request* (e.g.
   `initialize` → `prompt.cancel`) can execute before that request completes.
   Request/response clients (the shipped UI) are unaffected; a client that
   pipelines a control request against not-yet-established state can observe
   the pre-state outcome (e.g. `DATABASE_UNAVAILABLE`). Previously the serial
   loop guaranteed the state-establishing request ran first.
4. **Shutdown drain bound**: EOF/shutdown now waits at most 10 s for
   already-read requests, then cancels them (previously an in-flight request
   was awaited without bound). Turns/resources are still torn down by
   `Gateway.close()`.

---

## Verification

Commands (venv `.venv`, test DB `MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test`,
`-p no:cacheprovider`):

```
pytest tests/test_holistic_l5_gateway_2026_10_15.py          # 5 passed  (logs/l5_h20_red.txt RED → l5_h20_h21_green_final.txt GREEN)
pytest <17 gateway/stdio + services/compression files>       # 262 passed (logs/l5_focused_all.txt)
pytest <16 adjacent auto-compaction/turn files>               # 181 passed, 1 skipped (logs/l5_adjacent_compaction.txt)
ruff check <changed files>                                    # All checks passed (logs/l5_ruff.txt)
```

Suites included in the 262: `test_holistic_l5_gateway_2026_10_15.py`,
`test_gateway.py`, `test_r4_083_gateway_standalone.py` (real
`python -m machina.gateway` subprocess incl. token disclosure + shutdown),
`test_gateway_connectors.py` (real stdio wire), `test_gateway_images.py`,
`test_deep_gateway_cleanup.py`, `test_fm1_regressions.py`,
`test_wave3_hotfix.py`, `test_turns_ownership.py`, `test_audit_shutdown.py`,
`test_deep_session_cache.py`, `test_r4_082_gateway_providers.py`,
`test_compression.py`, `test_compaction_checkpoint.py`,
`test_handoff_regressions.py`, `test_production_readiness_p1.py`,
`test_transcript_export.py`.

Mid-lane regression caught and fixed by those suites: three pre-existing
`test_handoff_regressions.py` tests pass duck-typed sessions without
`provider`/`model` attributes; the resolver reads them with `getattr` so the
services change stays compatible (`logs/l5_focused_compression.txt` final:
142 passed).

## Evidence paths

* This report: `docs/verification/proofs/holistic_fixes_2026-10-15/L5_gateway.md`
* RED: `logs/l5_h20_red.txt`, `logs/l5_h21_red.txt`
* GREEN: `logs/l5_h20_green.txt`, `logs/l5_h21_green.txt`,
  `logs/l5_h20_h21_green_final.txt`, `logs/l5_focused_all.txt`,
  `logs/l5_adjacent_compaction.txt`
* Ruff: `logs/l5_ruff.txt`

Not committed/pushed per lane rules. No production files outside
`sessions.py` / `services.py` / `__main__.py` were modified; one wire test
(`test_gateway_connectors.py`) was adapted as called out above.
