---
name: cost-optimization
description: Cost optimization — token budgeting, prompt caching, model selection, batch processing, cost
  monitoring, cache-aware eviction
triggers:
- cost
- token budget
- cache
- prompt caching
- batch
- optimization
version: 1.0.0
schema_version: 2
skill_id: cost-optimization
task_families:
- maintenance
- backend-api
capabilities:
- performance
- observability
- configuration
aliases:
- cost
- token-budget
- prompt-caching
- llm-cost
- token-usage
stack:
  languages:
  - python
  frameworks:
  - tiktoken
  - litellm
  independent: true
preconditions:
- Token usage or spend is observable per session or agent
- Model and prompt choices are under your control
- A budget or cost target is defined
required_tools:
- tiktoken
exclusions:
- No LLM token spend is involved
- Latency-only optimization with no cost goal
- Redis cache mechanics rather than LLM spend (use redis-caching)
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

# Cost Optimization

## Core Skills

| Skill | Practice |
|---|---|
| Token budgeting | Token budget system with tiktoken |
| Prompt caching | Cache the system instructions and skill content |
| Model selection | Route simple tasks to cheaper models |
| Batch processing | Batch embedding requests |
| Cost monitoring | Token and budget usage per session/agent (`machina usage`) |
| Cache-aware eviction | Eviction that keeps prompt cache warm |

## Key Resources
- [OpenAI Prompt Caching](https://platform.openai.com/docs/guides/prompt-caching)
- [LiteLLM](https://github.com/BerriAI/litellm)
- [ephemeral-context](https://github.com/lkzppm/ephemeral-context)

## Practice Project
Build a cost optimization system: track token usage per session, implement prompt caching, route tasks to cost-effective models.
