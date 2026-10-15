# R4-019 — Terminal timeout plumbing + typed timeout message — evidence

## Decision (cap)

**Honour the schema: the loop now budgets up to 300 s** for `terminal`, matching
`tools/terminal.py` (schema advertises "max 300", handler validates `1 <= timeout <= 300`).
`tools/terminal.py` is unchanged — changing schema+handler down to a 60 s cap would have
removed a documented capability for legitimate long commands. The cap is the documented
constant `_TERMINAL_CAP = 300.0` (`machina/core/agent.py`, next to `_TOOL_TIMEOUTS`).

## Root cause

`_run_one` computed a static `tool_timeout` from `_TOOL_TIMEOUTS` (`terminal` → 60.0) and
wrapped every call in `asyncio.wait_for(..., timeout=tool_timeout)`. The requested
`timeout` argument never reached the budget, so `timeout > 60` was cancelled by the loop
at 60 s; `asyncio.wait_for` raises a message-less `TimeoutError`, which the generic
handler stringified into the literal `Error: `. Terminal's own
`Command timed out after Ns` was only reachable when its inner timer beat the loop
(`N <= ~59`).

## Fix (`machina/core/agent.py`, +6 lines; 2048 → 2054)

1. `_TERMINAL_CAP = 300.0` — documented hard cap matching the schema/handler.
2. In `_run_one`, after the MCP branch already threading `_spec` timeouts:
   `elif tool_name == "terminal": tool_timeout = min(max(int(_spec.get("timeout", 60)), 1), _TERMINAL_CAP)`
   (`_spec["timeout"]` is the same authorization-bound value that is digested at
   `agent.py:997`; floor 1 keeps out-of-schema requests reaching the handler's typed
   `ValidationError` instead of a zero-budget `wait_for` kill).
3. In the tool-failure handler: `if isinstance(e, asyncio.TimeoutError): e = f"tool '{tool_name}' timed out after {tool_timeout:g}s"`.
   Non-timeout errors take the identical path as before (`logger.exception` →
   `audit_log("tool_call_error", error=str(e))` → `result = f"Error: {e}"`); the audit
   row for timeouts now carries the typed text.

## Proof (E2E: real REST/SSE route + TurnEngine + agent loop + broker/store + real DB + real terminal subprocess; fake provider only at the factory boundary)

New test: `tests/test_tool_timeout_contract.py` (9 tests). `machina_test` only.

### RED — against the pre-fix tree (scratch copy, working tree untouched)

`R4-019_red_pytest.txt` → **8 failed, 1 passed** (the 1 pass is the default-path test):

```
tests/test_tool_timeout_contract.py:200: assert 'Error: Comma... out after 2s' == "Error: tool ... out after 2s"
tests/test_tool_timeout_contract.py:242: assert 'Error: ' == "Error: tool ...out after 60s"
tests/test_tool_timeout_contract.py:239: AssertionError: [60.0]        # requested 59/60/90/300 all recorded a 60.0 budget
tests/test_tool_timeout_contract.py:263: AssertionError: [60.0]        # out-of-schema requests, same static budget
```

Streamed frames, unfixed, request `{command: "sleep 30", timeout: 2}`:

```
{"type": "tool_result", "tool": "terminal", "result": "Error: Command timed out after 2s", "id": "terminal-1"}
```

`R4-019_red_60s_probe.txt` — pre-fix, real time, request **90 s** (command `sleep 120`):

```
RED-PROBE elapsed=60.3s result='Error: '
1 failed in 61.01s
```

### GREEN — fixed tree

`R4-019_green_pytest.txt` → **9 passed in 6.12s**:

- request 2 s: cancelled at the requested bound (5/5 repeats: 2.82/2.76/2.78/2.75/2.66 s;
  `pgrep` shows no orphaned `sleep`), model-visible result is exactly
  `Error: tool 'terminal' timed out after 2s` (both the SSE `tool_result` frame and the
  messages of the next provider call); stream still ends `done` + `data: [DONE]`.
- default path: `_TOOL_TIMEOUTS["terminal"] == 60.0` unchanged; `echo` (no `timeout` arg)
  executes normally, no timeout text.
- boundaries 59/60/90/300: the real `wait_for` receives exactly those budgets (recorded
  at the `ToolRegistry.execute` seam; wait compressed for wall-clock) and the typed
  message carries the same seconds.
- out-of-schema 0 / -3 / 1000: handler's typed error
  `timeout must be an integer between 1 and 300, got N` reaches the model; budget clamps
  to `[1, 300]` so the handler (not `wait_for`) reports it.

`R4-019_green_90s_probe.txt` — fixed tree, real time, request **90 s**:

```
RED-PROBE elapsed=90.3s result="Error: tool 'terminal' timed out after 90s"
1 passed in 90.90s
```

## Commands

```
# RED (scratch copy of the tree, pre-fix agent.py)
MACHINA_TEST_DATABASE_URL=... python -m pytest tests/test_tool_timeout_contract.py -q -p no:cacheprovider
MACHINA_TEST_DATABASE_URL=... python -m pytest tests/test_tool_timeout_contract.py::test_red_probe_90s_request_under_unfixed_60s_cap -q -s

# GREEN / checks (main tree)
MACHINA_TEST_DATABASE_URL=... python -m pytest tests/test_tool_timeout_contract.py -q -p no:cacheprovider   # 9 passed
MACHINA_TEST_DATABASE_URL=... python -m pytest tests/test_tool_timeouts.py tests/test_fm8_timeouts.py tests/test_chaos.py -q -p no:cacheprovider   # 37 passed
MACHINA_TEST_DATABASE_URL=... python -m pytest tests/test_architecture_layers.py tests/test_a5_turn_flow.py -q   # 38 passed
MACHINA_TEST_DATABASE_URL=... python -m pytest tests/test_model_context_limits.py tests/test_execution_context_boundary.py tests/test_final_pass_fixes.py -q -p no:cacheprovider  # 19 passed
.venv/bin/ruff format --check machina/core/agent.py tests/test_tool_timeout_contract.py   # 2 files already formatted
.venv/bin/ruff check machina/core/agent.py tests/test_tool_timeout_contract.py           # All checks passed
```

Ratchet: `machina/core/agent.py` = 2054 lines ≤ 2055 (`test_a5_turn_flow` shrink assertion)
and ≤ 2057 (`GOD_FILE_CEILINGS`).

## Residual risks

- Any `TimeoutError` that escapes a tool is now labelled with the loop budget seconds.
  For a tool that raises its own early `TimeoutError` (internal `wait_for`/`asyncio.timeout`
  without a local catch) the message names the loop budget, not the inner one; the message
  is still typed, and `terminal` itself raises `ToolError` for its inner timer, so the
  asserted path is unaffected. No silent swallowing anywhere.
- At the same requested `T`, the loop's `wait_for` and terminal's inner timer fire
  together; the loop-level cancellation wins deterministically (5/5 probe), so terminal's
  own `Command timed out after Ns` is now the rarer branch — both are model-visible error
  results, never `Error: `.
- Boundary 59/60/90/300 proof compresses the wait (recorder at the `ToolRegistry.execute`
  seam); uncompressed real-time evidence covers 2 s (green) and 60/90 s (red/fixed probe).
- The guard-timeout digest still normalises `timeout=0` to 60 (`agent.py:997`’s `or 60`),
  while the wait budget floors at 1 — both only matter for out-of-schema requests, which
  the handler rejects before any execution.
