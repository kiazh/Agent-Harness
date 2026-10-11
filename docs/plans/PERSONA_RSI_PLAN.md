# Persona RSI Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task in the current session. The user explicitly requested uninterrupted implementation; no additional design approval or commit is required.

**Goal:** Make persona and harness behavior improve from bounded, durable execution evidence and feedback, with inspectable revisions and rollback.

**Architecture:** Extend the existing growth store and shared agent lifecycle.
A focused RSI module owns metadata, routing statistics, and revision evaluation.
The existing retrieval bandit and skill relevance router retain their boundaries.

**Tech Stack:** Python 3.11+, PostgreSQL/asyncpg, existing pytest suite; TypeScript TUI.

**Spec:** [Persona RSI design](PERSONA_RSI_DESIGN.md)

## Global constraints

- No extra per-turn provider call; no raw text/arguments retained by RSI.
- 50 revisions, 500 episodes, 400-token strategy context, multipliers 0.8–1.2.
- Persona promotion needs three human-rated episodes of the active trial revision.
- Memory actions remain retrieve/noop; permissions and tool allowlists remain authoritative.
- No commits/deployment; only the dedicated `agentharness_test` database.

## Review focus

- Concurrent ratings/completions must not double-train or mix agent owners.
- A newer human note must not disappear when an older agent trial is rejected.
- Unfinished/paused/cancelled turns and unsupported outcome claims must not earn success.
- Learned weights must not activate unrelated skills or expose restricted tools.
- Revision pruning and process restart must preserve the active rollback baseline.

### Task 1: Durable evidence and revision engine

Files: `ah/memory/rsi.py`, `ah/db/growth_schema.sql`, `ah/memory/growth.py`,
`ah/core/config.py`, `tests/test_rsi.py`.

Interfaces: `GrowthStore.rsi_status(agent_id)`, `rollback(agent_id, revision)`,
`teach(..., origin='human')`, `begin(...)` returning an RSI decision;
`complete(..., outcome=None, skills=None)` records structured evidence.

- [x] Write failing real-DB tests for revisions, trial retention/rollback,
  automatic versus human evidence, once-only updates, restart, bounds, and isolation.
- [x] Run tests and record the missing behavior.
- [x] Implement additive schema and transactional revision/episode helpers.
- [x] Pass the new tests and existing personality-growth tests.

### Task 2: Shared agent and capability adaptation

Files: `ah/core/agent.py`, `ah/skills/runtime.py`, `ah/skills/workflow.py`,
`ah/tools/memory.py`, `tests/test_rsi_agent.py`, `tests/test_rsi.py`.

Interfaces: RSI decision contains domain, strategy, bounded skill/tool weights;
automatic skill context reports actual activated names through a per-turn list.
ContextVars hold per-turn metadata; tool definitions stay within the allowlist.

- [x] Write failing live-agent tests for both loop modes, future adaptation,
  provider-call count, restricted capabilities, cancellation, and approval pauses.
- [x] Record only safe tool names, success/failure counts, tokens, and iterations.
- [x] Integrate decision context, weighted relevant skill ranking, available-tool
  ordering, and agent-origin note versions; preserve task-local state.
- [x] Pass new and existing agent/skill/persona/delegation regressions.

### Task 3: Management end to end

Files: `ah/gateway/features/growth.py`, `ah/gateway/features/__init__.py`,
`ah/cli/growth.py`, `ui/src/features/growth.ts`, `ui/src/protocol.ts`,
`ui/test/features.test.ts`, backend management tests.

- [x] Write failing session-bound gateway/CLI and interactive TUI tests.
- [x] Add status/history and rollback using the same store, with validated revisions.
- [x] Keep explicit `/growth` argument commands; bare `/growth` offers actions.
- [x] Verify UI and backend management behavior, including malformed/foreign IDs.

### Task 4: Whole harness verification and documentation

- [x] Run focused RSI/persona/skill/lifecycle tests and touched-file Ruff checks.
- [x] Run the full backend suite with `AGENT_HARNESS_TEST_DATABASE_URL` set only
  to `postgresql://postgres:postgres@localhost:5432/agentharness_test`.
- [x] Run `npm --prefix ui run check`; fix regressions with red tests.
- [x] Update README, protocol documentation, HANDOFF, and this plan with evidence
  and actual limitations. Run `git diff --check`; report no unverified claims.


## Evidence ledger — 2026-10-11

Checkout: `main`, HEAD `dc77017` (`profile`), previous commits `0331634`,
`9ef4ed4`. Existing MCP/installer edits were preserved. No commits, pushes,
deployments, git configuration changes, or production DB access occurred.

Red/green evidence:

- Initial store tests: 8 failures for missing revision/evidence behavior, then green.
- Initial routing/agent tests: 4 missing-interface/behavior failures, then green.
- Initial gateway/CLI management tests: 2 missing-status failures, then green.
- Interactive UI tests: 4 failures before menu/form/restoration implementation.
- Fresh review: 10 regressions reproduced missing baseline restoration, disabled
  and same-text human-choice preservation, additive proxy/human memory updates,
  missing typed budget failures, and nonzero shell exits without error prefixes.
- Restoration provenance: 3 failures across manual rollback, trial rejection,
  and import/original-baseline restoration; fixed by snapshot episode membership.
- In-flight policy replacement: 2 failures, then 2 passes after completion
  reactivated its newly applied delta atomically.
- Additional coverage verifies explicit manual skill attribution without automatic
  skill loading, prompt/revision consistency during a concurrent note edit,
  and actual task cancellation in both agent loops after successful tool use.
- Final fresh static review reported no remaining material finding.

Verification commands (all run from the repository root unless specified):

```sh
AGENT_HARNESS_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agentharness_test .venv/bin/python -m pytest -q tests/test_rsi.py tests/test_rsi_agent.py tests/test_rsi_management.py --tb=short -p no:cacheprovider
# 43 passed in 3.58s.
AGENT_HARNESS_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agentharness_test .venv/bin/python -m pytest -q --tb=short -p no:cacheprovider
# 2092 passed, 5 skipped in 77.19s.
npm --prefix ui run check
# Exit 0: typecheck, 138 passed / 5 environment-gated skips, eslint.
.venv/bin/python -m ruff check ah/core/agent.py ah/core/config.py ah/core/models.py ah/memory/growth.py ah/memory/rsi.py ah/skills/runtime.py ah/skills/workflow.py ah/tools/memory.py ah/tools/terminal.py ah/gateway/features/growth.py ah/gateway/features/__init__.py ah/cli/growth.py tests/test_rsi.py tests/test_rsi_agent.py tests/test_rsi_management.py tests/test_features_split.py
# All checks passed.
.venv/bin/python -m ruff format --check ah/core/agent.py ah/core/config.py ah/core/models.py ah/memory/growth.py ah/memory/rsi.py ah/skills/runtime.py ah/skills/workflow.py ah/tools/memory.py ah/tools/terminal.py ah/gateway/features/growth.py ah/gateway/features/__init__.py ah/cli/growth.py tests/test_rsi.py tests/test_rsi_agent.py tests/test_rsi_management.py tests/test_features_split.py
# 16 files already formatted, line-length 100.
.venv/bin/python -m bandit -r ah/memory/rsi.py ah/memory/growth.py -ll
# Exit 0: 0 findings, including low severity.
git diff --check
# Exit 0.
```

Broader focused growth/skills/persona/API checks also passed **94 tests** before
adding the final in-flight and actual-cancellation cases. The backend suite
includes `test_ui_checks_with_real_gateway_restart`, which sets the opt-in
Python/test-DB environment and runs the actual Node client against restarted
Python gateways. It verifies persisted notes, history, and restoration without
a live provider. Standalone `npm run check` intentionally skips those five tests.

The first full run failed only the exact RPC method inventory assertion because
`growth.rollback` was newly registered. Its expected contract now includes that
method. A later run had 2083 passes and one DB setup deadlock during overlapping
full/focused database test commands. Running DB suites sequentially resolved it;
no implementation retry workaround was added. Both affected tests passed at
clean HEAD `dc77017` in a disposable detached worktree (**2 passed, 0.46s**):

```sh
# Run in the temporary clean-HEAD checkout, with PYTHONPATH set to that checkout:
AGENT_HARNESS_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agentharness_test /Users/shadow/Agent-Harness1/.venv/bin/python -m pytest -q tests/test_features_split.py::test_all_feature_methods_accessible_after_split tests/test_scheduler_coverage.py::test_finish_cron_reschedules_from_expression --tb=short -p no:cacheprovider
```

The worktree was removed after comparison. The final current-tree run passed
both tests and the complete suite; no unresolved failure remains in these checks.

Implementation rulings: revision 0 is a valid restore target; human confirmation
of an unchanged trial note establishes a stable revision; disabled RSI preserves
human management versioning; pending policy provenance is represented by bounded
episode IDs rather than duplicated delta arrays. Failed/truncated shell execution
and terminal budget/provider/iteration failure cannot earn positive evidence.

## Boundaries and next work

The requested runtime loop is implemented across the shared agent lifecycle and
its management surfaces. This is not proof that more use always improves quality:
operational proxies are noisy, three-rating persona evaluation is heuristic,
and no comparative quality/cost benchmark was run. Domain routing is intentionally
simple; feedback is shared by the installation's agent, not private per user.
A restored policy is a snapshot, not a replay of every historical counterfactual.
Retention is bounded: old episodes and revisions may no longer be rateable/restorable.

No LLM weight training or unattended application source rewriting/deployment was
implemented. Existing learned procedural-skill proposals retain their review
workflow. MCP agent-loop exposure, OAuth, connector management UI, and release/
production gates remain as recorded in the separate connector/release documents.
Do not equate RSI integration with completion of those independent projects.
