---
name: background-jobs
description: "Background jobs & task queues — workers, scheduling, retries with backoff, idempotency, dead-letter queues, at-least-once delivery"
triggers:
  - "background jobs"
  - "task queue"
  - "job queue"
  - "celery"
  - "cron"
  - "worker"
  - "retry"
version: 1.0.0
---

# Background Jobs & Task Queues

## Core Skills

| Skill | Practice |
|---|---|
| Enqueue | Hand off slow work so the request path stays fast |
| Workers | Horizontally scaled consumers with graceful shutdown |
| Retries | Bounded exponential backoff plus jitter; cap the attempts |
| Idempotency | Make a re-delivered job safe to run twice |
| Scheduling | Delayed and recurring jobs with a single owner per slot |
| Dead-letter | Park poisoned jobs with context for inspection |
| Visibility | Queue depth, age and failure rate as first-class metrics |
| Ordering | Per-key serialization only where the domain demands it |

## Pitfalls
- At-least-once delivery is the norm, so non-idempotent handlers duplicate work.
- Retrying a permanent failure forever hides bugs and floods the queue.
- Unbounded queues delay detection; alert on age, not just depth.
- A worker that ignores shutdown in-flight loses jobs mid-processing.

## Practice Project
Build one queue with a worker, exponential-backoff retries, an idempotency key, a dead-letter queue, and dashboards for depth, age and failures; then kill a worker mid-job and prove the task still completes exactly once in effect.

## Key Resources
- [Celery](https://docs.celeryq.dev/)
- [BullMQ](https://docs.bullmq.io/)
- [Enterprise Integration Patterns — Idempotent Receiver](https://www.enterpriseintegrationpatterns.com/)
