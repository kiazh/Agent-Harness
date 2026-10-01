---
name: multi-agent
description: "Multi-agent coordination — agent messaging, worktree isolation, shared context bus, task distribution, result aggregation, identity gating, non-monotonic reasoning, proxy-based integration"
triggers:
  - "multi-agent"
  - "bridge"
  - "opencode"
  - "codex"
  - "shared context"
  - "worktree"
  - "identity"
version: 1.0.0
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
Build a multi-agent bridge: AgentHarness dispatches task to OpenCode, OpenCode writes result to external_context, AgentHarness reads and reviews.
