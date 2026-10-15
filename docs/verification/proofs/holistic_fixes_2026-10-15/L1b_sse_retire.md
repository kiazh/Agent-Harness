# L1b — SSE retire join bounded (H01 residual)

Snapshot: `3ef6d5b` + working tree (uncommitted, like the other lanes). Finding
source: the H01 residual callout in
[L1_core_lifecycle.md](L1_core_lifecycle.md) ("Residual outside my files").

Lane files: `machina/api/routes/streaming.py` + new lane tests only
(`tests/test_holistic_l1b_sse_retire.py`). The join reuses
`background_tasks.await_joined_bounded()` exactly as landed by L1 — no second
implementation, and `machina/core/background_tasks.py` /
`machina/core/turn_engine.py` were not modified.

| Finding | Reproduced | Fix | Status |
| --- | --- | --- | --- |
| H01 residual (P1) | Yes — a wedged step blocked `retire_execution` before `close_turn` | Bounded join + quarantine; `close_turn` always proceeds | Fixed |

All tests were run with
`MACHINA_TEST_DATABASE_URL='postgresql://postgres:postgres@localhost:5432/machina_test'`
and `-p no:cacheprovider`.

---

## The residual

Pre-fix, the SSE timeout/cancel path spawned `retire_execution`, which awaited
the cancelled step task through the unbounded `await_joined` **before**
reaching `close_turn()` (`streaming.py`, now L425–448). `TurnEngine.close()` is
bounded (L1), but the transport never got there: a step that absorbs
cancellation wedged the retire task forever, so `close_turn()` never ran — the
owned provider stayed open and the session claim was never released while its
renewal loop kept renewing it. (L1's H01 fix covers the engine's own source;
this is the transport-side step task in front of it.)

## Reproduced — yes

`tests/test_holistic_l1b_sse_retire.py::test_wedged_step_retire_join_is_bounded`
drives the real streaming route (`create_app` endpoint, real `TurnEngine`,
DB-less claim registry) with a provider whose stream absorbs every
cancellation; only the provider boundary and the session lookup are stubbed.

Pre-fix, with the transport bound overridden to 0.2 s for the pin
([logs/L1b_red_tests.txt](logs/L1b_red_tests.txt)):

```
E  AssertionError: retire_execution did not reach close_turn within the bound:
   a wedged step must be quarantined, never awaited unbounded
1 failed, 2 passed
```

`retired_in_time=False`, the step was not quarantined, and the claim stayed
held — the retire task was still parked on the unbounded join (the RED run's
finally releases the double, which is why the pre-fix retire then completes).
Both control cases pass pre-fix (they pin unchanged behavior).

## Fix

- New module constant `RETIRE_JOIN_TIMEOUT_SECONDS = 5.0` (read at call time;
  5.0 mirrors the engine's `source_close_timeout_s` default).
- `retire_execution` now joins the active step with
  `await_joined_bounded(active, timeout=RETIRE_JOIN_TIMEOUT_SECONDS, label="app.retire_execution")`
  — L1's helper and contract: a cancellation delivered while joining is
  consumed once and the join gets the bound again; at the deadline the step is
  **quarantined** (cancelled, held by the module strong-reference set, its
  eventual failure observed by the done callback) and `close_turn()` proceeds.
- `close_turn(driver_settled=...)`: when the step was still running at the
  deadline, the engine stream generator is **not** `aclose()`d — closing a
  running async generator raises `RuntimeError: aclose(): asynchronous
  generator is already running` ([logs/L1b_aclose_probe.txt](logs/L1b_aclose_probe.txt));
  the driver's own unwind closes it, exactly the rule L1's `_close_source`
  applies to the engine's source. The engine close, provider close and claim
  release run either way.
- The engine for this stream is constructed with
  `source_close_timeout_s=RETIRE_JOIN_TIMEOUT_SECONDS`, so the two bounded
  waits on the same pathological step share one knob and the worst case stays
  ~2× the bound instead of unbounded.

Unchanged: steps that settle within the 0.1 s post-cancel window take the
direct path exactly as before; steps that settle later are still joined before
retirement (no early claim release); a settled step whose stream failed is
still reported through `app.retire_execution`.

Post-fix, the same pin is green
([logs/L1b_green_tests.txt](logs/L1b_green_tests.txt)): retirement, provider
close and claim release all happen **while the step is still wedged**, the step
is in `background_tasks._quarantined_joins`, and the retirement task exits
cleanly (no forced `aclose`).

## Tests

`tests/test_holistic_l1b_sse_retire.py` — three pins on the real route +
real engine:

1. **wedged step**: bounded retirement (overridden 0.2 s bound), wrapped
   provider closed, claim re-takable, step quarantined and still running, and
   the retire task finishes without error;
2. **control — resistant-but-settling step**: still joined before retirement
   (claim held while the execution runs, released after it stops); unchanged;
3. **control — well-behaved cancellable step**: direct close path, no
   `turn.cleanup_pending` frame; unchanged.

## Verification

| Command (abridged) | Result | Log |
| --- | --- | --- |
| RED: new pins with pre-fix wiring (final test file) | 1 failed (wedged pin), 2 passed (controls) | [red](logs/L1b_red_tests.txt) |
| `pytest tests/test_holistic_l1b_sse_retire.py tests/test_turn_completion_lifecycle.py` | 7 passed | [green](logs/L1b_green_tests.txt) |
| `pytest tests/test_holistic_l1b_sse_retire.py tests/test_turn_completion_lifecycle.py tests/test_holistic_h01_turn_source_close.py tests/test_sse_stream_adversarial.py` | 18 passed | [log](logs/L1b_sse_focused.txt) |
| `pytest tests/test_turn_engine.py tests/test_f07_streaming_growth_scope.py tests/test_claim_failure_boundary.py tests/test_fm1_regressions.py tests/test_final_review_2026_10_15_batch3_gateway_obs_memory.py tests/test_rest_ownership_loss.py` | 64 passed | [log](logs/L1b_adjacent_suites.txt) |
| `pytest tests/test_api_route_table.py tests/test_api.py tests/test_deep_http_cleanup.py tests/test_handoff_regressions.py tests/test_http_approval_resume.py tests/test_tool_timeout_contract.py tests/test_production_smoke.py` | 91 passed | [log](logs/L1b_http_suites.txt) |
| 3× loop of the new pins + `test_turn_completion_lifecycle.py` | stable, 7 passed each | [log](logs/L1b_flakiness_loop.txt) |
| `ruff check` + `ruff format --check` on lane files | clean | [log](logs/L1b_ruff_check.txt) |
| `tests/test_architecture_layers.py` | 22 passed, 4 failed — all out-of-lane (below) | [log](logs/L1b_arch_out_of_lane.txt) |

## Callouts

- **Worst-case delay bound.** With defaults, a cancellation-resistant step now
  delays `close_turn()` by at most ~0.1 s (settle window) + 2 × 5.0 s (retire
  join, then the engine's bounded source close on the same task) ≈ 10.1 s
  before the engine's (already bounded) provider close and claim release run;
  pre-fix this was unbounded (claim held forever). `RETIRE_JOIN_TIMEOUT_SECONDS`
  is read at call time (test-overridable, default 5.0).
- **Out-of-lane observation.** The 4 `tests/test_architecture_layers.py`
  failures above already exist in this working tree and are owned by
  concurrent lanes' files: deferred-import census 720 > 717, `core/agent.py`
  2269 > 2267 lines, `core/provider/copilot.py` 443 > 431, provider package
  5015 > 5003. This lane contributes zero: `streaming.py`'s deferred-import
  count is unchanged (11 → 11, verified with the guard's own AST counter) and
  it has no god-file ceiling entry.
- No commits; only lane-owned files were modified (`machina/api/routes/streaming.py`,
  the new lane test, and this proof's logs). `background_tasks.py` /
  `turn_engine.py` carry only L1's pre-existing uncommitted work.
