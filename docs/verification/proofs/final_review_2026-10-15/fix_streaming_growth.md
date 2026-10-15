# F07 — HTTP streaming drops growth state between generator steps — fix evidence

Lane: fix (final review). Sole writer of `machina/api/routes/streaming.py` and one
new evidence test file. `machina/core/agent.py` was **not** modified.

Finding: `docs/verification/CODEBASE_REVIEW_FOLLOWUP_2026-10-15.md` → F07 (P2).

## Root cause (confirmed)

`machina/api/routes/streaming.py::_stream_session` pulls every agent-generator
step in a **fresh asyncio Task** (`_owned_step` → `asyncio.ensure_future(stream.__anext__())`).
An asyncio Task runs in a **copy** of its creator's context, so a ContextVar
written by a step task never propagates back to the driver or into the next
task. The agent sets the growth/RSI ContextVars while *preparing* the first
step, so later steps and the `done` step (which runs `_complete_growth`) read
the ContextVar defaults — growth episodes never complete.

Exact ContextVar names / set points in `machina/core/agent.py` (read only):

| ContextVar | Set / mutate points | Read points |
|---|---|---|
| `_growth_episode` (agent.py:91) | `:574` (reset None), `:644` (`decision["id"]`), `:1570` (reset in `_complete_growth`) | `:1567` (`_complete_growth`) |
| `_rsi_choice` (agent.py:92) | `:575` (None), `:645` (`decision.get("rsi")`), `:1591` (None) | `:324`, `:716` |
| `_rsi_skills` (agent.py:93) | `:576` (`[]`), `:1592` (None); mutated `:1107` (`.append`) | `:717`, `:1104-1107`, `:1580` |

### Constraint check (required)

**All three ContextVars are currently SET inside the agent, not merely read.**
Per the lane rule, this is reported here and the fix proceeds **driver-side
only** (no `agent.py` edit).

The R4-010 pattern could not be applied literally (move the `set`/`reset` into
the driver): the *values* are derived inside `_prepare_context`
(`growth_store.begin(...)`), so the driver has no value to set before the first
step runs. The driver-side equivalent that preserves the same ownership
principle is to run every step task in **one shared execution Context**, so the
agent's writes survive for the rest of the turn.

## Fix (smallest safe, driver-side)

`machina/api/routes/streaming.py`:

- One per-turn `contextvars.Context` (`_step_context`).
- `_owned_step` runs stream steps via
  `loop.create_task(awaitable, context=context)` once armed; before arming
  (the agent-build step) it keeps the previous per-task copy.
- Armed once, right before the drive loop, **after** the transport-owned
  `resume_approval_var` is set, and copied from the driver context that already
  owns the R4-010 turn scope. Every agent step (including the `done` handler)
  now shares growth/RSI state written during preparation.

Steps are always awaited to completion before the next task is created, so one
Context is never entered concurrently. CLI (`cli/__init__.py`) and gateway
(`gateway/server.py`) drivers are single-task and untouched — no behavior change
for them.

`try/except ValueError` reset tolerance is not needed here: the driver never
takes a `ContextVar` token in the shared Context (the shared context is created,
not entered), so there is no cross-context `reset` to tolerate. The existing
tolerant `end_turn_scope` / `resume_approval_var.reset` (R4-010) are unchanged.

## RED → GREEN

New focused test: `tests/test_f07_streaming_growth_scope.py`.

It drives the **real route** (`create_app` + `ASGITransport`, real test DB) with
a fake agent (only `agent_factory.build_agent_for_session` is replaced). The
fake mimics the real lifecycle across three generator steps:

1. set `_growth_episode` / `_rsi_choice` / `_rsi_skills` (as `_prepare_context` does),
2. append to `_rsi_skills` (as a tool step does — `agent.py:1104-1107`),
3. in the `done` step, read them (as `_complete_growth` does) and record.

RED (pre-fix): `observed_episode is None` — the done step read the ContextVar
default. GREEN (post-fix): all three values survive.

- RED log: `fix_streaming_growth_RED.log` — `1 failed` (`observed_episode == None`).
- GREEN log: `fix_streaming_growth_focused.log` — `122 passed`.

## Verification

All commands use `MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test`
(local `machina_test` only).

| Command | Result | Log |
|---|---|---|
| `pytest tests/test_f07_streaming_growth_scope.py` (pre-fix) | **1 failed** (RED) | `fix_streaming_growth_RED.log` |
| `pytest` focused set: `test_f07_streaming_growth_scope + test_sse_stream_adversarial + test_turn_engine + test_turnscope_cache + test_wiring_parity + test_api + test_api_bugfixes + test_api_route_table + test_a5_turn_flow + test_turn_completion_lifecycle` | **122 passed** | `fix_streaming_growth_focused.log` |
| `pytest tests/test_f07_streaming_growth_scope.py` ×5 (flake check) | 5 × **1 passed** | (transcript) |
| `pytest tests/test_cli_turn_ownership.py tests/test_gateway.py` (single-task drivers) | **39 passed** | `fix_streaming_growth_cli_gateway.log` |
| `ruff check` + `ruff format --check` on both changed files | clean | `fix_streaming_growth_ruff.log` |

Pre-existing, **unrelated** failures observed while sampling broader suites
(`test_rpc_gateway.py` ordering, `test_personality_growth.py::test_ui_checks...`
npm UI, `test_rsi_agent.py` restart) reproduce on a clean tree with this change
stashed — they are not caused by (and not touched by) this lane.

## Files changed

- `machina/api/routes/streaming.py` — driver-side shared step Context (F07).
- `tests/test_f07_streaming_growth_scope.py` — new focused route test (RED→GREEN).

Diff: `fix_streaming_growth.diff`.

## Verdict

**Fixed (driver-side), verified.** The HTTP streaming driver now owns one
per-turn execution Context so growth/RSI state written during preparation is
visible to later steps and the `_complete_growth` path. CLI/gateway single-task
drivers are unchanged. The agent still owns the growth ContextVar *set points*;
a future agent-side refactor could expose explicit growth-scope entry, but it is
not required for this finding and was out of scope for this lane.
