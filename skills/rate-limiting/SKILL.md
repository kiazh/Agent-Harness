---
name: rate-limiting
description: Rate limiting & throttling — token bucket, leaky bucket, sliding window, quotas, backpressure,
  retry-after, fair sharing
triggers:
- rate limit
- rate limiting
- throttling
- token bucket
- quota
- backpressure
- leaky bucket
version: 1.0.0
schema_version: 2
skill_id: rate-limiting
task_families:
- backend-api
capabilities:
- performance
- error-handling
- api-design
aliases:
- rate-limit
- throttling
- quotas
- backpressure
- token-bucket
stack:
  languages: []
  frameworks: []
  independent: true
preconditions:
- an HTTP endpoint, gateway, or internal call path to protect
- a shared counter store when running multiple instances
- a traffic profile or load test to size limits
required_tools:
- redis
exclusions:
- CDN caching or static asset delivery
- authentication and authorization design
- database connection pool sizing
sections:
- id: core-skills
  heading: Core Skills
  stage: implement
  requires_sections: []
- id: pitfalls
  heading: Pitfalls
  stage: review
  requires_sections: []
- id: practice-project
  heading: Practice Project
  stage: verify
  requires_sections:
  - core-skills
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
---

# Rate Limiting & Throttling

## Core Skills

| Skill | Practice |
|---|---|
| Token bucket | Allow bursts up to capacity, refill at a steady rate |
| Leaky bucket | Smooth output at a fixed drain rate |
| Sliding window | Precise counts without the fixed-window boundary spike |
| Quotas | Per-key budgets (user, tenant, API key) with separate pools |
| Backpressure | Signal overload upstream instead of dropping work silently |
| Client contract | Return 429 with Retry-After and a clear error body |
| Distributed state | Share counters atomically across instances |
| Fairness | Prevent one tenant from starving the others |

## Pitfalls
- Fixed windows let a burst straddle the boundary and double the limit.
- Per-instance counters multiply the effective limit by instance count.
- Returning 429 without Retry-After makes well-behaved clients retry hot.
- Rate limiting the edge but not internal calls still lets a bug cascade.

## Practice Project
Add a token-bucket limiter to one endpoint with shared state, per-key quotas, a 429 response carrying Retry-After, and a load test that shows the limit holds across multiple app instances.

## Key Resources
- [Stripe — rate limiters](https://stripe.com/blog/rate-limiters)
- [RFC 6585 — 429 Too Many Requests](https://www.rfc-editor.org/rfc/rfc6585)
- [Redis — rate limiting patterns](https://redis.io/docs/latest/develop/use/patterns/)
