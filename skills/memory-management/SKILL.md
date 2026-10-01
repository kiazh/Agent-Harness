---
name: memory-management
description: "Memory management — context compression, 5-tier retrieval, AKL, reversible eviction, dedup, cross-session memory, feedback loop, decay-aware recall, cache-aware eviction, memory-as-file, trust tiers"
triggers:
  - "memory"
  - "compression"
  - "eviction"
  - "retrieval"
  - "decay"
  - "forget"
  - "remember"
version: 1.0.0
---

# Memory Management

## Core Skills

| Skill | Practice |
|---|---|
| Context compression | Rolling summary with LLM |
| 5-tier retrieval | Cache → full-text → embedding → LLM → agentic |
| AKL | Maturity tiers, importance scoring, recency decay |
| Reversible eviction | 2-tier: summarize → archive verbatim |
| Memory dedup | Embedding-based dedup with cosine threshold |
| Cross-session memory | Long-term memories persist across sessions |
| Memory feedback loop | Per-operation feedback |
| Decay-aware recall | Similarity × importance × recency |
| Cache-aware eviction | Keep prompt cache warm |
| Memory-as-file | Markdown + frontmatter + wikilinks |
| Trust tiers | Governance for shared multi-agent memory |

## Key Resources
- [ByteRover paper](https://arxiv.org/abs/2604.01599)
- [Agent Zero Memory](https://arxiv.org/abs/2608.29606)
- [Blast Radius](https://arxiv.org/abs/2608.07440)
- [MemGPT](https://arxiv.org/abs/2310.08560)

## Practice Project
Build a memory manager: store memories with importance scores, retrieve top-k by relevance, decay old memories, promote frequently accessed ones.
