---
name: chaos-engineering
description: Chaos engineering — fault injection, resilience testing, game days, blast radius control,
  steady-state hypotheses, rollback
triggers:
- chaos
- chaos engineering
- fault injection
- failure injection
- game day
- blast radius
- resilience
version: 1.0.0
schema_version: 2
skill_id: chaos-engineering
task_families:
- testing
- maintenance
capabilities:
- observability
- error-handling
aliases:
- chaos
- fault-injection
- gameday
- resilience-testing
- blast-radius
stack:
  languages: []
  frameworks:
  - chaos-monkey
  - litmuschaos
  independent: true
preconditions:
- a staging environment resembling production
- measurable steady-state SLIs
- a tested abort and rollback path
required_tools: []
exclusions:
- diagnosing an active production outage (use incident-response)
- unit or integration test authoring
- first launch of an unproven system in production
sections:
- id: core-skills
  heading: Core Skills
  stage: plan
  requires_sections: []
- id: pitfalls
  heading: Pitfalls
  stage: review
  requires_sections: []
- id: practice-project
  heading: Practice Project
  stage: implement
  requires_sections:
  - core-skills
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
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
