# L1 — core lifecycle fixes (H01, H03, H11)

Snapshot: `64fc75f` (working tree, no commits). Findings source:
[CODEBASE_REVIEW_HOLISTIC_2026-10-15.md](../../CODEBASE_REVIEW_HOLISTIC_2026-10-15.md).
Lane files: `machina/core/turn_engine.py`, `machina/core/background_tasks.py`,
`machina/core/scheduler.py`, `machina/core/agent.py`, plus new lane tests only.

| Finding | Reproduced | Fix | Status |
| --- | --- | --- | --- |
| H01 (P1) | Yes — synthetic hang blocked claim release | Bounded source shutdown + quarantine; release independent | Fixed |
| H03 (P2) | Yes — real `PermissionError` on overridden job | Persisted-owner reconciliation before the run | Fixed (behavior callout) |
| H11 (P2) | Yes — provider kwargs lacked `temperature` | Configured temperature forwarded on both paths | Fixed |

All tests were run with
`MACHINA_TEST_DATABASE_URL='postgresql://postgres:postgres@localhost:5432/machina_test'`
and `-p no:cacheprovider`.

---

## H01 — cancellation can hang before session ownership is released

**Reproduced — yes.** A synthetic agent whose stream finalizer suppresses
cancellation (and a driver that absorbs cancellation while awaiting the next
source event) leaves `TurnEngine._close_source()` waiting forever through the
unbounded `await_joined`; `engine.close()` never reaches `_release_claim()`, so
`turns.end_turn` is never called and (in a transport) the renewal loop keeps the
session claim alive.

- Standalone reproduction (public API only):
  [L1_h01_repro.py](L1_h01_repro.py) — pre-fix output
  [logs/L1_h01_red_repro.txt](logs/L1_h01_red_repro.txt):
  `close_outcome='hang'`, `claim_released_before_gate=[]`.
- Focused tests pre-fix fail (parameter absent pre-fix; the behavioral hang is
  proven by the standalone repro and by the quarantined task assertions
  post-fix): [logs/L1_h01_h11_red_tests.txt](logs/L1_h01_h11_red_tests.txt).

**Fix.** `TurnEngine._close_source()` now bounds both shutdown stages with a new
`source_close_timeout_s` (default `5.0`) and proxies them through a new owned
primitive, `background_tasks.await_joined_bounded()`:

- A cancellation delivered while joining is consumed once and the operation
  gets the bound again (same contract as the claim release), so a healthy finalizer
  still finishes before the cancellation propagates (bounded).
- When the bound expires the operation is **quarantined**: cancelled, kept in a
  module-level strong-reference set, and its eventual failure observed via a
  done callback. `_close_source` then returns so `close()` proceeds to provider
  close and the shielded claim release regardless of source completion.
- If the driver was quarantined, `source.aclose()` is skipped (the
  actively-executing generator would raise; the driver's own unwind closes the
  source). `close()` now also records ordinary source failures instead of
  raising them out of cleanup (`record_failure` sites `source_shutdown`,
  `source_driver`, `source_close`).
- `await_joined` itself is deliberately unchanged (15 other call sites rely on
  its join-before-propagate semantics via the bounded cleanup tails that own
  their own timeouts).

Post-fix reproduction output (default bound, release happens while the finalizer
is still unsettled): [logs/L1_h01_green_repro.txt](logs/L1_h01_green_repro.txt):
`join deadline exceeded ... work quarantined`, `close_outcome='bounded'`,
`claim_released_before_gate=['owner-token']`.

Files: `machina/core/turn_engine.py` (constructor L175/L186, `_close_source`
L342, `close()` L533), `machina/core/background_tasks.py` (quarantine registry
L133, `await_joined_bounded` L155).

Tests: `tests/test_holistic_h01_turn_source_close.py` — resistant finalizer
(stream-then-engine close order), resistant driver while a step is active, and
an ordinary `aclose` failure that must be reported, not raised.

Callouts:
- Additive API: new `TurnEngine(source_close_timeout_s=5.0)` parameter; existing
  callers are unaffected. In the pathological case the claim is released within
  ~one bound (≤5s by default) instead of never.
- Residual, outside this lane's file ownership: the SSE route's
  `retire_execution` path still `await_joined(active)`s a wedged step task
  without a deadline before reaching `close_turn()`
  (`machina/api/routes/streaming.py:418`). The engine it then calls is bounded,
  but a wedged SSE *step* can still delay that transport's release. Flagged for
  routing; not touched here.

## H03 — scheduled jobs selecting another agent fail ownership validation

**Reproduced — yes, exactly as reviewed.** A real `JobRunner.run_due_once()`
tick for a `harness`-owned session with a `researcher` job builds the
researcher agent, then `ReActAgent.run` reloads the durable session and raises
`PermissionError: Session does not belong to the running agent`
(`agent.py::_prepare_context`); the tick records `status="error"` with that
error, and the provider is never called. Evidence:
[logs/L1_h03_red_test.txt](logs/L1_h03_red_test.txt).

**Fix — choice: explicit persisted-owner reconciliation at execution time.**
`JobRunner._reconcile_session_owner(job)` (called from `_execute` right before
`agent.run`) makes the durable `sessions.agent_id` match the executed job
identity and refreshes the session cache, so the agent's own ownership check
accepts the run. This completes the R5-C execution model (“the JOB’s agent
identity governs the run”; R5-C commit `49e592c`).

Why this over an “authorized child session/delegation flow”:

- the scheduler's job session is the durable home of the job's context
  (heartbeat/interval prompts continue it); a fresh child per run would only
  receive the 1200-token delegation handoff and would move the transcript away
  from the session the job belongs to;
- approval pauses for jobs are keyed by `(session_id, digest)` and resumed by
  re-running the job (`jobs_resume`); a new child session per run would break
  that revalidation (the approved grant would no longer match the new session);
- it is by far the smaller change: no new session lifecycle, no schema
  addition, every existing scheduler/pause pin keeps its semantics.

Behavior/API callout: an overridden job now **durably reassigns the session
owner** to the job's selected agent. Subsequent direct turns on that session
run as that agent (consistent with the chunks the run already writes under the
job identity). This is deliberate, documented in the method docstring, and
audited at run time (`job_agent_owner_reconciled`; job creation already emits
`job_agent_override`). Matching-identity jobs remain exact no-ops; DB-less and
injected-factory paths are untouched.

Files: `machina/core/scheduler.py` (`_execute` call site L997,
`_reconcile_session_owner` L1133, `_build_agent` docstring note L1085).

Tests: `tests/test_holistic_h03_scheduler_ownership.py` — real tick, real
session/job rows, real agent loop + ownership check on an isolated schema
(shared-`db`-singleton pattern from `tests/test_r4_105_caller_scope_real_run.py`),
only the LLM provider boundary stubbed. Asserts the job completes idle, the
provider really ran, and the durable owner is reconciled.

## H11 — the configured temperature is not forwarded to agent calls

**Reproduced — yes.** With `config.temperature` set to non-default values, the
provider boundary observed only `messages`, `tools`, `max_tokens` — no
`temperature` — on both the completion (`usage_store.complete_call` →
`provider.complete`) and streaming (`provider.stream_complete`) paths, so the
provider default stayed in effect. Evidence:
[logs/L1_h01_h11_red_tests.txt](logs/L1_h01_h11_red_tests.txt).

**Fix.** `BaseReActAgent._call_llm_with_retry` and `_stream_llm_with_retry`
now pass `temperature=float(config.get("temperature"))` alongside the existing
`max_tokens`. The value is already range-validated by configuration
(`0.0..2.0`, env/session overrides validated at `config.get` time).

Files: `machina/core/agent.py` (L385, L447).

Tests: `tests/test_holistic_h11_temperature.py` — fake providers capture kwargs
per path; asserts the configured temperature (and the unchanged `max_tokens`)
reaches the provider.

---

## Verification

| Command (abridged) | Result | Log |
| --- | --- | --- |
| `pytest tests/test_holistic_h01_turn_source_close.py tests/test_holistic_h03_scheduler_ownership.py tests/test_holistic_h11_temperature.py` | 6 passed | [green](logs/L1_h01_h03_h11_green_tests.txt) |
| pre-fix runs of the same files | RED (hang / `PermissionError` / missing temperature) | [H01+H11](logs/L1_h01_h11_red_tests.txt), [H03](logs/L1_h03_red_test.txt) |
| `pytest tests/test_turn_engine.py tests/test_background_drain.py tests/test_r5_007_scheduler_identity.py tests/test_scheduler.py tests/test_scheduler_coverage.py tests/test_scheduler_model_pin.py tests/test_scheduler_scripts.py tests/test_scheduler_usage.py tests/test_deep_scheduler.py tests/test_fm4_scheduler.py tests/test_review_scheduler_clock.py` | 128 passed | [log](logs/L1_focused_suites.txt) |
| `pytest tests/test_model_context_limits.py tests/test_provider_args_contract.py tests/test_provider_usage.py tests/test_usage.py tests/test_audit_regressions.py tests/test_audit_shutdown.py tests/test_deep_gateway_cleanup.py tests/test_deep_runtime_cleanup.py tests/test_deep_runtime_shutdown.py tests/test_final_review_2026_10_15_cleanup.py tests/test_cli_turn_ownership.py tests/test_turns_ownership.py tests/test_usage_cleanup.py tests/test_rest_ownership_loss.py tests/test_approvals_ownership.py` | 119 passed | [log](logs/L1_adjacent_suites.txt) |
| `pytest tests/test_tool_timeout_contract.py tests/test_r4_022_denied_outcome.py tests/test_sse_stream_adversarial.py tests/test_final_review_2026_10_15_batch3_gateway_obs_memory.py tests/test_deep_job_deadline.py tests/test_agent_factory_memory.py tests/test_authority_merge.py tests/test_turnscope_cache.py` | 60 passed | [log](logs/L1_engine_adjacent.txt) |
| `pytest tests/test_final_pass_fixes.py tests/test_final_review_fix_core_agent_context.py tests/test_history_condensation.py tests/test_personality_growth.py tests/test_failed_delegation_usage.py tests/test_delegation_contracts.py tests/test_delegation_boundaries.py tests/test_delegation_outcomes.py` | 116 passed | [log](logs/L1_agent_paths.txt) |
| `pytest tests/test_audit_remediation.py tests/test_production_readiness_p2.py` (job pause/resume owners) | 57 passed | [log](logs/L1_scheduler_pause.txt) |
| `ruff check` + `ruff format --check` on all changed files + lane tests + repro | clean | [log](logs/L1_ruff_check.txt) |
| H01 standalone repro, 5× flakiness loop of the H01 pins | stable | green repro log above; 5× `3 passed` |

No commits; only lane-owned files were modified (other diffs in the working
tree belong to concurrent lanes).
