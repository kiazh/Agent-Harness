---
name: context-runtime
description: Context runtime development — context runtime design, token-budget enforcement, reversible
  operations, context pipeline
triggers:
- context runtime
- turn loop
- token budget
- reversible
- pipeline
version: 1.0.0
schema_version: 2
skill_id: context-runtime
task_families:
- backend-api
- maintenance
capabilities:
- performance
- configuration
aliases:
- context-engine
- turn-loop
- token-budget
- context-pipeline
stack:
  languages:
  - python
  frameworks: []
  independent: true
preconditions:
- The agent turn loop is implemented in this codebase
- Per-model token limits are known
- Context sources such as retrievers and tools are identifiable
required_tools: []
exclusions:
- Only prompt wording changes are needed
- Retrieval quality tuning (use rag-pipeline)
- Cross-session memory storage (use memory-management)
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
