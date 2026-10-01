---
name: context-runtime
description: "Context runtime development — context runtime design, token-budget enforcement, reversible operations, context pipeline"
triggers:
  - "context runtime"
  - "turn loop"
  - "token budget"
  - "reversible"
  - "pipeline"
version: 1.0.0
---

# Context Runtime Development

## Core Skills

| Skill | Practice |
|---|---|
| Context runtime design | Own the full turn loop |
| Token-budget enforcement | Slot-based allocation |
| Reversible operations | Auditable context transformations |
| Context pipeline | Retriever → Re-ranker → Compressor → TokenBudget |

## Key Resources
- [SR2](https://github.com/terminus-labs-ai/sr2) — context runtime as middle tier
- [context-engine](https://github.com/Emmimal/context-engine) — clean pipeline design
- [jabr](https://github.com/Moshe-ship/jabr) — reversible context operations

## Practice Project
Build a context runtime layer that manages the full agent turn loop with token budget enforcement and reversible operations.
