---
name: heartbeat-system
description: Heartbeat system development — APScheduler, pg_cron, hybrid approach, outbox pattern, session
  liveness detection
triggers:
- heartbeat
- idle
- scheduler
- apscheduler
- pg_cron
- outbox
version: 1.0.0
schema_version: 2
skill_id: heartbeat-system
task_families:
- backend-api
- maintenance
capabilities:
- database
- error-handling
- configuration
aliases:
- heartbeat
- idle-detection
- scheduler
- pg-cron
- apscheduler
- outbox
stack:
  languages:
  - python
  - sql
  frameworks:
  - apscheduler
  - pg_cron
  - postgresql
  independent: false
preconditions:
- PostgreSQL with the pg_cron extension available
- a Python service that can run APScheduler
- a sessions table with activity timestamps
required_tools:
- python
- postgresql
exclusions:
- generic cron authoring outside PostgreSQL
- browser-side idle or presence detection
- one-off background scripts with no delivery guarantee
sections:
- id: core-skills
  heading: Core Skills
  stage: implement
  requires_sections: []
- id: practice-project
  heading: Practice Project
  stage: verify
  requires_sections:
  - core-skills
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
---

# Heartbeat System

## Core Skills

| Skill | Practice |
|---|---|
| APScheduler | Interval-based heartbeat with APScheduler |
| pg_cron | DB-native scheduling for heartbeat chunks |
| Hybrid approach | APScheduler triggers + pg_cron DB operations |
| Outbox pattern | Reliable message delivery |
| Session liveness | Detect idle vs. active sessions |

## Key Resources
- [APScheduler docs](https://apscheduler.readthedocs.io/)
- [pg_cron docs](https://github.com/citusdata/pg_cron)
- [Outbox pattern](https://microservices.io/patterns/data/transactional-outbox.html)

## Practice Project
Build a heartbeat system that detects idle sessions and inserts reminder chunks. Implement both APScheduler and pg_cron approaches, compare reliability.
