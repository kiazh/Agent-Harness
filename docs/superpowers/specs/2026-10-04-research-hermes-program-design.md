# Research and Hermes Capability Program

**Status:** Working design, 2026-10-04. This is a capability program, not a claim
that the research results or Hermes parity have been achieved.

## Goal

Make AgentHarness a usable implementation of all five research directions in
`docs/research-agenda-2026-realignment.md` and the major Hermes Agent
capabilities documented by Nous Research. Preserve AgentHarness's PostgreSQL,
Python gateway, TypeScript TUI, optional budgets, and existing API rather than
porting Hermes's internal storage or UI code verbatim.

External accounts and hosted backends remain optional. Local and Docker users
must be able to run the core system without a paid provider or platform token.

## Baseline and gaps

| Track | Working foundation | Completion evidence still needed |
| --- | --- | --- |
| Reversible context | Byte-preserving archive, resurrection, compression, LoCoMo evidence retrieval | Adaptive compaction, measured token cost and answer quality against eviction baselines |
| Identity defense | Beliefs, keyed provenance, validation, drift history, quarantine | Shared-memory bus that gates every recipient; adversarial and benign longitudinal traces |
| Persona memory | Factual/persona separation and emotion topology | Learned or calibrated persona conditioning; multi-persona human and task evaluation |
| RL memory policy | Six actions, rewards, offline finite-action trainer | Representative rollout corpus, reproducible joint model-training pipeline, held-out task and safety evaluation |
| Soul Spec | Schema, merge, package validation, adapters | Live import/export runs in target frameworks and published conformance cases |
| Hermes learning loop | Skills, learning command, curator and memory store | Post-turn review, staged skill edits, read-before-write, skill-use feedback, learning history |
| Hermes recall | PostgreSQL full-text indexes and session title search | Agent-callable cross-session transcript discovery, scroll and read with agent scoping |
| Hermes orchestration | Cron, delegation, gateway, streaming and usage budgets | Structured child output, steering/cancellation, durable task coordination, per-job model and delivery |
| Hermes ecosystem | Plugin hooks, tools, TUI, Docker, OpenRouter | Optional channel adapters, MCP lifecycle, toolsets, analytics, additional execution backends where tested |

Existing research documents are source material, not completion checklists:
some predate shipped prototypes or describe SQLite even though this repository
uses PostgreSQL. Each milestone below must update the relevant status table.

## Architecture

1. Keep PostgreSQL as the canonical durable store. Search both live context and
   archived context; never make recall depend on an LLM-generated summary.
2. Expose each durable capability through a small service boundary, then wire
   the gateway, API, TUI, and agent tool surfaces where the use case requires
   them. Reuse the current session and permission model; cross-session recall
   must be scoped to the requesting agent.
3. Run learning work after the user-facing turn on bounded budgets. Proposed
   memory or skill writes are scanned, reviewable, and reversible. A failed
   review cannot fail the foreground response.
4. Keep research methods behind explicit configuration until a benchmark and
   rollback test justify runtime activation. Store dataset versions, hashes,
   seeds, model/provider, cost, and baseline results with every experiment.
5. Treat each external platform, provider, and sandbox as an adapter with a
   capability contract and a missing-credentials test. No integration silently
   switches models or incurs paid usage.

## Delivery order and acceptance

### A. Cross-session recall and learning foundation

- Add scoped transcript search and anchored context windows over live and
  archived chunks, then expose it to the agent and gateway. Prove recall after
  compaction and after process restart.
- Add a bounded post-turn review with explicit triggers and a cost cap. Stage
  memory/skill suggestions; add a read-before-write guard and approval path.
- Measure recall quality, latency, and review precision on labeled examples.

### B. Memory and context research

- Complete adaptive compaction and compare token use, answer quality, and
  resurrection quality on LoCoMo plus local long-running tasks.
- Make shared-memory publication and delivery pass identity gates for each
  recipient; test forged provenance, drift, benign changes, and quarantine.
- Evaluate persona conditioning across multiple personas and long sessions;
  ship learned conditioning only after it beats the rule-based baseline.

### C. Automation and interaction

- Extend scheduled jobs with model pinning, delivery state, retries and a
  no-agent mode; extend delegated work with structured output and cancellation.
- Add usage insights and an observable learning history to the existing TUI.
- Add optional channel adapters and tool/terminal backends in tested batches,
  starting with a common adapter contract and one end-to-end connector.

### D. Research training and portability

- Build a reproducible rollout and model-training path for memory actions,
  compare it with the finite-action and no-memory baselines, then run an online
  safety and rollback trial before runtime selection.
- Run Soul Spec conformance and round trips inside actual target frameworks;
  publish mismatches and versioned test fixtures.

For every deliverable: a failing behavior test precedes the implementation;
integration tests cover the public surface; migrations are restart-safe; usage
limits and secret handling are verified; the benchmark records a baseline and
the new result. A checked box means the public behavior and evidence exist,
not merely that a class or schema was added.

## First implementation slice

Cross-session recall is the first slice. `SessionManager.search()` currently
searches titles only, while live and archived chunks already have PostgreSQL
full-text indexes. Implement agent-scoped discovery over transcript text and
an anchored window/read interface without an LLM dependency. Keep existing
title search behavior compatible. Follow with a gateway method, agent tool,
and TUI entry point in separate tested changes.

## Sources checked

- [Hermes Agent repository](https://github.com/nousresearch/hermes-agent)
- [Hermes memory guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- [Hermes skills guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- [Hermes messaging guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/)
- [Hermes tools guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools)
