# LangGraph Decision for AgentHarness

**Reviewed:** 2026-10-04  
**Status:** CLOSED — 2026-10-04. Native PostgreSQL path retained as the
runtime; LangGraph remains an optional research dependency behind the
`workflow-trial` extra.

## What changed

The 2026-10-01 version of this document recommended against LangGraph because
multi-agent work and long-running approvals were outside the then-current scope.
That premise is obsolete. AgentHarness now has sequential and parallel
delegation in `ah/core/orchestrator.py`, a durable job queue in
`ah/core/scheduler.py`, and a memory approval queue. The expanded
[research and Hermes program](superpowers/specs/2026-10-04-research-hermes-program-design.md)
also calls for durable child coordination, cancellation and reviewable skill
changes. Those requirements justify a fresh comparison.

## Current boundary

`ReActAgent` still executes each turn inside a process. `sessions` and
`context_chunks` preserve conversation data, and jobs survive a restart, but
there is no persisted program counter for a partially executed agent turn. A
crash can leave a tool call or approval workflow without a resumable run state.
The existing memory approval queue is separate from a suspended tool call.
Delegation exists, but child execution has no graph-level checkpoint or
automatic resume. These are distinct from conversation persistence.

LangGraph offers thread-scoped checkpoints, persistent PostgreSQL savers,
and interrupts that can pause and resume a graph. Its store is a separate
cross-thread memory primitive; adopting a checkpointer would not replace
AgentHarness's memory, usage accounting, or context archive. An in-memory
saver would not satisfy process-restart recovery.

## Final Decision (2026-10-04)

**Keep the native PostgreSQL path as the runtime.** LangGraph stays an
optional research dependency behind the `workflow-trial` extra. The
migration boundary is an isolated per-workflow opt-in if a real delegated
approval workflow ever proves a recovery benefit.

**Reason:** The trial measured 3.618 ms mean for native start-plus-approval
versus 18.608 ms for LangGraph — a 5.1x latency overhead for the same
scenario. The native path is simpler, faster, and already integrated into
AgentHarness's existing PostgreSQL infrastructure. LangGraph's checkpoint
tables and dependency footprint are not justified for the current workflow
scope. The trial did not exercise usage budgets, gateway streaming, audit
parity, or external side effects, so the production decision gate was not
passed.

**What would change the decision:**
- A real delegated approval workflow that requires pause/resume across
  process restarts and proves a recovery benefit that the native path
  cannot provide.
- LangGraph demonstrating significantly better recovery semantics for
  multi-step delegated tasks that the native row-state approach cannot
  match.
- A production requirement for graph-level checkpointing that justifies
  the operational complexity of checkpoint tables, schema migrations, and
  the additional dependency.

## Trial result (2026-10-04)

`ah.research.workflow_trial` now runs the same paused approval and idempotent
PostgreSQL insert through a native row-state implementation and an isolated
LangGraph graph with `AsyncPostgresSaver`. Tests cover denial, cancellation,
agent ownership, repeat resume, reopening the checkpointer, and a separate
Python process starting and resuming the graph. The Windows trial requires
`psycopg[binary]` and a Selector event loop. The optional dependency group is
`workflow-trial`; it is not part of the production install.

A five-run local comparison, after setup, measured 3.618 ms mean for native
start-plus-approval versus 18.608 ms for LangGraph; the graph wrote 20
checkpoint rows. This small benchmark is only a mechanism check and cannot
establish a stable latency ratio. Full machine/version data are in
[`research/results/workflow-trial-2026-10-04.json`](../research/results/workflow-trial-2026-10-04.json).

The trial has **not** passed the production decision gate: its effect is a
transactional database insert, not a delegated agent tool; it does not yet
exercise usage budgets, gateway streaming, audit parity, or external side
effects. The native path is simpler for this case. Keep LangGraph optional
until a real delegated approval workflow proves a recovery benefit that
justifies its checkpoint tables and dependency footprint.

An isolated workflow is the migration boundary. Converting all agents,
sessions, and scheduled jobs at once would create duplicate persistence
models before recovery semantics are understood. If the trial succeeds,
rollout should be opt-in per workflow with an explicit checkpoint retention
policy and a migration path for paused runs.

**Decision recorded:** The native PostgreSQL path is retained as the
runtime. LangGraph remains an optional research dependency. See
[Final Decision](#final-decision-2026-10-04) above.

## Findings and corrections to the earlier document

| Earlier claim | Current finding |
| --- | --- |
| AgentHarness is single-agent | `Orchestrator` already supports sequential and parallel delegation. |
| PostgreSQL session persistence equals durable execution | Transcript storage does not record the current execution step or resume a tool call. |
| Human approval is absent | Memory approval exists; approval and resume of arbitrary agent actions do not. |
| LangGraph prevents self-hosting | LangGraph has a PostgreSQL checkpointer and can run without a hosted LangSmith service; the integration still adds dependencies and operational work. |
| `create_react_agent()` is the reason to adopt | The requirement is recovery and interruption. Current LangChain guidance uses `create_agent` for a standard agent; custom LangGraph nodes remain available. |
| LangGraph supplies exactly-once tool execution | Resume/replay can repeat side effects unless the application makes them idempotent. |

The earlier document also used unsupported fixed package-size and popularity
estimates, a weighted score without measured data, and a stale test count.
Those figures are excluded from this decision. Benchmark results belong in
`docs/research-evaluation.md` once the trial runs.

## Primary sources

- [LangGraph persistence and checkpointers](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)
- [LangChain agents and LangGraph relationship](https://docs.langchain.com/oss/python/learn)
