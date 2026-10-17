---
name: testing-qa
description: Testing & QA — async testing, integration testing, property-based testing, benchmarking,
  load testing
triggers:
- test
- pytest
- async test
- integration
- benchmark
- load test
version: 1.0.0
schema_version: 2
skill_id: testing-qa
task_families:
- testing
capabilities:
- test-authoring
- performance
aliases:
- testing
- qa
- pytest
- integration-tests
- load-testing
- benchmarking
stack:
  languages:
  - python
  frameworks:
  - pytest
  - hypothesis
  - locust
  independent: false
preconditions:
- A codebase with a runnable test harness
- Python test dependencies installed
required_tools:
- shell
exclusions:
- Manual exploratory QA with no automation
- Live incident triage of a failing system
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
