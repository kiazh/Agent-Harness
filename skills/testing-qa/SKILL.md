---
name: testing-qa
description: "Testing & QA — async testing, integration testing, property-based testing, benchmarking, load testing"
triggers:
  - "test"
  - "pytest"
  - "async test"
  - "integration"
  - "benchmark"
  - "load test"
version: 1.0.0
---

# Testing & Quality Assurance

## Core Skills

| Skill | Practice |
|---|---|
| Async testing | pytest-asyncio for asyncpg queries |
| Integration testing | Mock LLM, test ReAct loop with tools |
| Property-based testing | Hypothesis for context chunk serialization |
| Benchmarking | pgvector query latency over time |
| Load testing | 10 agents hitting same context bus |

## Key Resources
- [pytest-asyncio](https://pytest-asyncio.readthedocs.io/)
- [Hypothesis](https://hypothesis.readthedocs.io/)
- [Locust](https://locust.io/)

## Practice Project
Write tests for: context chunk CRUD, pgvector retrieval, skill trigger matching, agent message passing. Target: 80% coverage on core modules.
