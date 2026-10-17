---
name: multi-agent
description: Multi-agent coordination — agent messaging, worktree isolation, shared context bus, task
  distribution, result aggregation, identity gating, non-monotonic reasoning, proxy-based integration
triggers:
- multi-agent
- bridge
- opencode
- codex
- shared context
- worktree
- identity
version: 1.0.0
schema_version: 2
skill_id: multi-agent
task_families:
- backend-api
- maintenance
capabilities:
- api-design
- error-handling
- configuration
aliases:
- multiagent
- agent-bridge
- agent-coordination
- worktree-isolation
- shared-context
stack:
  languages:
  - python
  frameworks:
  - opencode
  - codex
  independent: false
preconditions:
- More than one agent or runtime must cooperate on a task
- A shared task store or message transport is available
- Git worktrees or isolated workspaces can be created
required_tools:
- git
exclusions:
- Single-agent workflow with no handoff
- Only prompt design for one agent is changing
- No shared state, queue, or result aggregation is needed
sections:
- id: core-skills
  heading: Core Skills
  stage: implement
  requires_sections: []
- id: key-resources
  heading: Key Resources
  stage: inspect
  requires_sections: []
- id: practice-project
  heading: Practice Project
  stage: verify
  requires_sections:
  - core-skills
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
---

# Multi-Agent Coordination

## Core Skills

| Skill | Practice |
|---|---|
| Agent messaging | agent_messages table + polling |
| Worktree isolation | Per-agent git worktrees |
| Shared context bus | All agents read/write same context |
| Task distribution | Work queue with SELECT FOR UPDATE SKIP LOCKED |
| Result aggregation | Result collector with conflict resolution |
| Identity gating | Validate shared memories against belief model |
| Non-monotonic reasoning | Rollback on conflict detection |
| Proxy-based integration | Zero-code cross-framework compatibility |

## Key Resources
- [OpenCode](https://github.com/anomalyco/opencode)
- [Codex CLI](https://github.com/openai/codex)
- [MetaGPT](https://github.com/geekan/MetaGPT)
- [ChatDev](https://github.com/OpenBMB/ChatDev)

## Practice Project
Build a multi-agent bridge: Machina dispatches task to OpenCode, OpenCode writes result to external_context, Machina reads and reviews.
