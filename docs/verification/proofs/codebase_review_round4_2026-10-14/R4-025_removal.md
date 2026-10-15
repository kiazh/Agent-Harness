# R4-025 — `max_iterations` removal + PB-1 flush hardening (2026-10-14)

Owner decision: **REMOVE** (enforce-alternative rejected). Removal IS the behavior
change and is recorded as such below. Do NOT commit (owner integrates).

## Root causes (one sentence each)

- `max_iterations` was validated (1–50/1–100), persisted (config/DB/YAML/soulspec/API)
  and documented, but **no code path ever read it** — both agent loops are
  `while True` (capacity/usage/stagnation stop the turn). Dead surface, removed.
- PB-1: `BaseReActAgent._flush_pending_chunks` (`machina/core/agent.py`, old
  `:1327-1332`) let `context_manager.add_chunks_batch` errors escape, so a
  context-store failure during `run()` (`:1602`/`:1693`) aborted the turn instead
  of returning the model's response.

## Fix (smallest safe change)

- `machina/core/agent.py`: dropped the `max_iterations` ctor param + attribute;
  `_flush_pending_chunks` now catches non-cancellation flush errors,
  `logger.warning`s (`Context flush failed (<Exc>); continuing turn`), retains
  pending chunks for a later retry, and clears only on success. `CancelledError`
  still propagates (cancellation-safe flush path unchanged).
- `machina/core/config.py` (3 bits only): `DEFAULTS["max_iterations"]`, the
  `(1, 100)` bounds entry, and the `Config.max_iterations` field removed.
  `config.set("max_iterations", …)` / gateway `config.set` now reject with
  unknown-key (`INVALID_PARAMS` on the gateway).
- `machina/core/agent_def.py`: `AgentDef.max_iterations` field, `to_dict`
  `maxIterations`, YAML legacy parsing/validation, and all SQL `max_iterations`
  references (SELECT/INSERT/UPDATE/RETURNING, save arg, `_row_to_def`) removed.
  Legacy YAML/DB rows carrying the key still load (unknown keys ignored).
- Soulspec mapping: `Config.max_iterations` + `from_dict`/`to_dict`/`to_agent_def`
  mapping (`schema.py`), `MachinaAdapter` + both Markdown exports
  (`adapters.py`), `_merge_config` (`merge.py`), `import_persona` carry-over
  (`lifecycle.py`), conformance validation + merge example (`conformance.py`).
- DB migration (no `connection.py` change needed): `agents` CREATE TABLE in
  `machina/db/schema.sql` no longer defines the column + idempotent
  `ALTER TABLE … DROP COLUMN IF EXISTS max_iterations` in **both** `schema.sql`
  (full-init path) and `soulspec_schema.sql` (runtime-upgrade path already wired
  into `initialize_schema`/`initialize_runtime_schema`).
- Tests: all `ReActAgent(…, max_iterations=N)` kwargs dropped; dead
  `mock_agent_def.max_iterations` / test-double assignments removed;
  soulspec/config/YAML pins reworked to surviving fields (model/tools/
  description/context_budget); `config.set` rejection of `max_iterations`
  pinned (`test_gateway_features.py`); absence pinned (`not hasattr`,
  `test_audit_regressions.py`; `test_no_max_iterations_gate_in_loops` untouched).
- `tests/test_chaos.py`: the 2 strict-xfail PB-1 markers
  (`TestDatabaseChaos::test_agent_handles_db_connection_drop`,
  `test_agent_handles_db_pool_exhaustion`) flipped to hard passes —
  **marker removal only, zero assertion changes**.

## Behavior changes (recorded)

1. `max_iterations` surface deleted: ctor/`AgentDef`/config/YAML/soulspec/API
   (`to_dict` `maxIterations`) no longer accept or emit it. Unknown-key errors
   where a typed contract exists (`Config.set`, gateway `config.set`,
   `AgentDef(...)`, `SoulSpec.Config(...)`); silent ignore for legacy YAML keys
   and old stored `soul_spec` JSON (which still deserialize).
2. Flush failures are now log-and-continue: a turn returns the model's response
   even when every context write fails (pending chunks retained for retry).
3. KNOWN CROSS-LANE BREAKAGE (not my files, flagged for gateway lane):
   `machina/gateway/features/agents.py:47` still passes `max_iterations=10` to
   `AgentDef(...)` → `agents.save` RPC now raises
   `TypeError: AgentDef.__init__() got an unexpected keyword argument
   'max_iterations'` (one-line fix: drop the kwarg). Failing until then:
   `test_bug_fixes.py::test_agents_save_allows_non_builtin_names` (expects
   `DatabaseError`, gets `TypeError`) and
   `test_multi_agent.py::TestGatewayAgents::test_list_save_delete`
   (`KeyError: 'result'` on the error response).
4. Residual references owned by other lanes (left untouched per fence):
   `ui/test/features.test.ts`, `ui/test/gateway.e2e.test.ts`,
   `ui/test/protocol-parity.test.ts` (UI lane), `FEATURE_LEDGER.md:86,202`
   + review docs (docs lane), historical session transcripts under
   `docs/verification/fixtures/` (evidence, do not rewrite).

## Proof

RED-first (pristine HEAD, isolated worktree):

- PB-1: `pytest tests/test_chaos.py::TestDatabaseChaos --runxfail` →
  `2 failed, 1 passed`; failures are the named reason:
  `Exception: Connection lost` / `Exception: Pool exhausted` escaping
  `agent.run` (see `R4-025_red_pb1.txt`, `R4-025_red_pb1-reason.txt`).
- Cap-removal: 38 `max_iterations` hits in owned prod files at HEAD
  (`agent_def.py:15`, `schema.py:5`, `conformance.py:5`, … — see
  `R4-025_red_surface.txt`); post-fix grep over the owned surface is clean
  except intentional pins (absence/rejection assertions) and the migration
  `DROP COLUMN` statements themselves.

GREEN (isolated worktree at HEAD + only this task's patch; main tree was
unusable for pytest at the time — a concurrent lane's half-finished
`context.py` edit broke the `reset_singletons` autouse fixture):

- `tests/test_chaos.py`: **26 passed** (incl. the 2 flipped PB-1 tests).
- Adjacent agent/config/soulspec suites (21 files, live postgres for DB-gated):
  `565 passed` (`R4-025_green_final.txt`); the only 2 failures are the
  gateway-lane breakage in §3 above (same root cause, verified
  `Gateway method agents.save failed — TypeError`).
- `test_multi_agent.py::TestOrchestrator::test_delegate_records_message_and_child_session`
  flaked once in 3 full runs (passes alone + in file runs; FakeAgent + shared-DB
  history assertion, none of this task's code paths) — recorded as pre-existing
  order-dependent flake, not a regression.
- DB migration on scratch DBs (`R4-025_migration.txt`): fresh `schema.sql` +
  `soulspec_schema.sql` → `agents` has **0** `max_iterations` columns, DROPs are
  NOTICE-only no-ops; legacy table (column + 1 row) → column dropped, row
  (`legacy-agent|kept`) preserved; rerun → `RERUN-OK`.
- `ruff check`: all passed. `ruff format --check`: 32 files already formatted
  (`R4-025_ruff.txt`).

## Files changed (30, uncommitted)

`machina/core/agent.py`, `machina/core/config.py`, `machina/core/agent_def.py`,
`machina/soulspec/{schema,adapters,merge,lifecycle,conformance}.py`,
`machina/db/schema.sql`, `machina/db/soulspec_schema.sql`,
`tests/test_{chaos,chaos_extended,comprehensive,deadlock_guard,orchestrator_tokens,gateway_images,integration,integration_permissions,handoff_regressions,parallel_tool_execution,multi_agent,soulspec,soulspec_bugfixes,audit_regressions,gateway_features,features_split,api,scheduler_model_pin,phase6,phase5_completion}.py`.
