# R4-063 / R4-064 (+ OTel stream-span follow-up) — evidence

Tree: `/Users/shadow/machina` (worktree with concurrent Round-4 work; no commit).
Primary files: `machina/observability/safe_logging.py`, `machina/observability/traces.py`;
span follow-up: `machina/core/agent.py` (instrument_stream only),
`machina/gateway/server.py`, `machina/cli/__init__.py`, `machina/api/routes/streaming.py`.
Tests: `tests/test_r4_063_064_observability.py` (new),
`tests/test_sse_stream_adversarial.py` (new OTel assertion).

---

## R4-063 — safe_logging: structured + guarded redaction

**Probe (unfixed):** `safe_logging.py:12-29` redacted only top-level strings; dict/list/
tuple args passed through untouched, and `redact_secrets` was called without a guard.

**RED** (`R4-063_064_red_pytest.txt`, original files restored from HEAD):

- `8 failed, 1 passed` — named reasons:
  - `assert 'sk-R4OBS0123456789abcdef' not in "payload {'a...` → nested dict/list secret leaked verbatim.
  - `RuntimeError: redactor down` escaped `logger.warning(...)` (unguarded redactor).
  - `KeyError: 'dropped_tool_spans'`, `AttributeError: ... no attribute 'dropped_turns'`
    (R4-064 side of the same run — see below).

**Root cause:** the record factory's `value()` had no container branch and no
`try/except` around the redactor call, so (a) secrets nested below a structured arg
were logged verbatim and (b) a failing redactor raised inside the logging call.

**Fix (smallest safe):**
- `value()` now walks `dict`/`list`/`tuple` recursively (keys included), bounded at
  depth 8 (cycles/absurd nesting → `[REDACTED]`, never the raw container).
- A redactor failure raises a private `_RedactionFailed`; the factory's outer guard
  masks the WHOLE record (`msg="[REDACTED]"`, `args=()`, `exc_info/exc_text=None`).
  Whole-record masking (not a per-string marker) is deliberate: a `%`-template
  replaced by a marker next to still-present args breaks `record.getMessage()`
  inside handlers — measured while developing (`TypeError: not all arguments
  converted during string formatting`). Fail-closed covers both.
- Non-`machina.*` loggers are untouched (pinned by test).

**GREEN** (`R4-063_064_green_pytest.txt`): `9 passed`.
Tests cover: nested dict/list/tuple + mapping-style args; raising redactor →
rendered message is exactly `[REDACTED]` and nothing escapes; cyclic arg masked at
the depth bound; non-machina namespace untouched. The raising-redactor test goes
through the real logger + caplog sink (matrix: real logger + sink capture).

---

## R4-064 — trace-ring eviction markers

**Probe (unfixed):** `_TURNS` ring evicted with a bare FIFO pop
(`traces.py:324-325` at review; the in-flight parent could be evicted while closed
turns were available) and `_append_span` counted dropped spans but not the
tool-call subset, so `turn_report.tool_calls.total` undercounted with no delta.

**RED** (same `R4-063_064_red_pytest.txt` run, original `traces.py`):

- `KeyError: 'dropped_tool_spans'` on a >500-tool-span turn (undercount unmarked).
- `AssertionError: in-flight parent was evicted while closed turns were available`
  after 130 child turns.
- `AttributeError: module 'machina.observability.traces' has no attribute 'dropped_turns'`.

**Fix:**
- `TurnTrace` gains `parent_turn_id`, `dropped_tool_spans`, `dropped_turns`.
- Span ring: evicted TOOL spans bump `dropped_tool_spans` (root attribute updated)
  so `turn_report` exposes the tool-call delta explicitly;
  `render_diagnostics` renders `tools: N (ok=…) (+K dropped)`.
- Turn registry: `_evict_turns()` evicts FINISHED turns first (FIFO); only when
  every retained turn is in flight is the oldest force-evicted, and every eviction
  is marked: process counter exposed as `dropped_turns()` (mirrors
  `dropped_reservations()`), a `turn_evicted` sink event carrying
  `{turn_id, ended, parent_turn_id}`, and the victim's still-retained parent gets
  `dropped_turns += 1` + root attribute. So an in-flight turn is never evicted
  without a marker, and a parent whose children were evicted carries the count.
- `turn_report` now returns `dropped_spans`, `dropped_tool_spans`, `dropped_turns`;
  diagnostics append `child turns evicted: N` when nonzero.

**GREEN** (`R4-063_064_green_pytest.txt`): `9 passed` —
`>128 child turns → parent report carries dropped markers and correct-or-marked
totals` (parent retained, `dropped_turns == expected`, evicted child `turn_report`
is None, survivor totals correct, every eviction announced with `ended=True`);
forced all-in-flight eviction marked (`ended=False`, exact victims);
tool delta counts only tool spans (alternating llm/tool eviction → 5 of 10).

Registry growth is still hard-bounded (`MAX_TURNS_RETAINED`); evictions are
prefer-finished, not unbounded retention.

---

## OTel stream-span follow-up (tracked under R4-064)

**RED** (`R4-063_064_red_span.txt`, unfixed `instrument_stream` span restored
temporarily): the H-SSE OTel assertion fails on the NORMAL multi-event path:
`AssertionError: ['Failed to detach context']` with the opentelemetry error record
(`opentelemetry.context` `logger.exception`, `ValueError: <Token ...> was created
in a different Context`). Mechanism: the span enters when a step task first
resumes the agent generator and is detached by whichever step task finalizes it.

**Fix:** span removed from `instrument_stream` (`machina/core/agent.py`); the
`agent.run_stream` span now wraps the driver loop in the two single-task drivers:

- `machina/gateway/server.py` — `with span("agent.run_stream", agent_id=…,
  session_id=…)` around the `engine.stream()` relay (inside the turn-scope block).
- `machina/cli/__init__.py` — same wrapper around the `aclosing(agent.run_stream(...))`
  drive loop.
Attributes preserved: `agent_id` (built agent) + `session_id`. Coverage matches the
old wrapper (the `done`-event growth/metrics hooks run inside the wrapped relay).

**SSE — stopped, deliberately (`R4-063_064_sse_span_attempt.txt`):** the equivalent
`with span(...)` around `_stream_session`'s drive loop was implemented and
measured: normal path clean, but the harness cancel path still logs
`Failed to detach context` — the async generator is resumed by one task and
finalized by another (`_owned_step` per-step tasks; Starlette may also close the
response cross-task), and unlike `end_turn_scope` there is no tolerant detach for
opentelemetry spans (its `context.detach` logs internally; tracing.py is outside
this task's write scope, and a queue/owner-task restructure is not a smallest-safe
change). Per the task's stop rule, the SSE span is not forced: the route carries a
precise code comment (`streaming.py`, turn-scope block) and owns no OTel span.

**GREEN** (`R4-063_064_green_span_harness_x3.txt`): H-SSE harness 3× → `4 passed`
each, with the strict assertion `no opentelemetry ERROR records` on both the normal
and cancel paths. Gateway/CLI-driven suites re-run with `--log-cli-level=ERROR`:
`0` occurrences of `Failed to detach context` (28 passed).

---

## Commands (final state)

```
MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test \
  .venv/bin/python -m pytest tests/test_sse_stream_adversarial.py \
    tests/test_metrics.py tests/test_fm8_trace_producers.py tests/test_trace_hierarchy.py \
    tests/test_observability_payload_boundary.py tests/test_audit_lifecycle.py \
    tests/test_fm6_traces.py -q -p no:cacheprovider            # 49 passed
  ... tests/test_architecture_layers.py tests/test_a5_turn_flow.py
                                                               # 38 passed
  ... tests/test_r4_063_064_observability.py                   # 9 passed
  ... tests/test_gateway.py tests/test_cli_turn_ownership.py \
      tests/test_turn_engine.py tests/test_rest_ownership_loss.py
                                                               # 65 passed
  ... tests/test_sse_stream_adversarial.py (3 consecutive runs) # 4 passed each
  ... tests/test_turnscope_cache.py tests/test_production_smoke.py \
      tests/test_wiring_parity.py tests/test_leaf_independence.py \
      tests/test_config_import_cycle.py tests/test_architecture_pairs.py \
      tests/test_fm7_cli.py tests/test_cli_turn_e2e.py tests/test_gateway_images.py
                                                               # 166 passed
  ... tests/test_trace_hierarchy.py tests/test_cli_turn_ownership.py \
      --log-cli-level=ERROR                                    # 0 detach logs
.venv/bin/python -m ruff format --check <touched files>         # clean
.venv/bin/python -m ruff check <touched files>                  # clean
wc -l machina/core/agent.py                                     # 2055 (cap 2057)
```

SSE harness runs are in `R4-063_064_green_span_harness_x3.txt`; RED/GREEN pytest
logs are the two files above (`R4-063_064_red_pytest.txt`,
`R4-063_064_green_pytest.txt`, `R4-063_064_red_span.txt`,
`R4-063_064_sse_span_attempt.txt`).

## Residual risks / notes

- `test_cli_turn_e2e.py::test_headless_full_mode_runs_routine_tool_without_pause`
  (another workstream's test) failed once inside a 166-test batch, then passed on
  re-run, in isolation, and as its own file. Not explained by this change set
  (span wrapping only); watched.
- The SSE transport now emits no `agent.run_stream` OTel span. Resume point: give
  the SSE driver a single-task owner (queue/owner-task) or a non-attaching span
  helper in `observability/tracing.py`, then re-run
  `test_stream_span_never_detaches_across_contexts`.
- `dropped_turns` is per-turn (evicted children of this turn); `dropped_turns()` is
  the process-wide eviction counter (like `dropped_reservations()`).
