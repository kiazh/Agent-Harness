---
name: chaos-engineering
description: "Chaos engineering — fault injection, resilience testing, game days, blast radius control, steady-state hypotheses, rollback"
triggers:
  - "chaos"
  - "chaos engineering"
  - "fault injection"
  - "failure injection"
  - "game day"
  - "blast radius"
  - "resilience"
version: 1.0.0
---

# Chaos Engineering

## Core Skills

| Skill | Practice |
|---|---|
| Steady-state hypothesis | Define the measurable normal before injecting anything |
| Fault injection | Latency, errors, CPU/memory pressure, network partition, dependency loss |
| Blast radius control | Start in staging; gate production with a small percentage |
| Abort conditions | Automatically stop when SLIs breach a threshold |
| Game days | Timed exercises with roles, a scribe and a follow-up list |
| Rollback | Every experiment ships with a tested, one-step revert |
| Learning loop | Turn each surprise into a runbook or a fix |

## Pitfalls
- Injecting without a hypothesis produces anecdotes, not evidence.
- Chaos in production without an abort switch risks an outage, not a test.
- Testing only components you already trust misses the real failure modes.
- Skipping the write-up loses the lesson the experiment paid for.

## Practice Project
Run one game day in staging: state a steady-state hypothesis, inject a dependency timeout, confirm graceful degradation, abort on breach, and record findings as runbooks and follow-up tickets.

## Key Resources
- [Principles of Chaos Engineering](https://principlesofchaos.org/)
- [Chaos Monkey](https://netflix.github.io/chaosmonkey/)
- [LitmusChaos](https://litmuschaos.io/)
