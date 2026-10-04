# LangGraph Decision for AgentHarness

**Reviewed:** 2026-10-04  
**Status:** Evaluation required for durable workflows; no production integration yet.

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

## Decision

Do **not** replace the existing ReAct loop merely to obtain a graph-shaped
implementation. Do evaluate LangGraph as the durable coordinator for one
specific workflow: a delegated task that pauses for a tool or skill-write
approval, survives a process restart, and resumes exactly once. Keep the
existing provider, tool registry, PostgreSQL context, audit and budget
boundaries inside the workflow nodes. A native checkpoint prototype should
run the same scenario for comparison.

The trial passes only if it demonstrates all of the following:

1. A run resumes after killing and restarting the worker, using PostgreSQL
   persistence rather than an in-memory saver.
2. An approval is linked to the exact proposed action and can be denied;
   cancellation and a second resume cannot execute the action again.
3. Tool side effects are idempotent or have a persisted execution receipt.
   LangGraph can replay a node after interruption, so a checkpoint alone
   cannot guarantee exactly-once effects.
4. Token and request budgets, audit events, agent ownership, and streaming
   events match the current gateway behavior.
5. The dependency, schema, latency, and recovery behavior are measured with
   a reproducible integration test. A failed trial leaves the current loop
   as the runtime and records the reason.

An isolated workflow is the migration boundary. Converting all agents,
sessions, and scheduled jobs at once would create duplicate persistence
models before recovery semantics are understood. If the trial succeeds,
rollout should be opt-in per workflow with an explicit checkpoint retention
policy and a migration path for paused runs.

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
