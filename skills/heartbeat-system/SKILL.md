---
name: heartbeat-system
description: "Heartbeat system development — APScheduler, pg_cron, hybrid approach, outbox pattern, session liveness detection"
triggers:
  - "heartbeat"
  - "idle"
  - "scheduler"
  - "apscheduler"
  - "pg_cron"
  - "outbox"
version: 1.0.0
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
