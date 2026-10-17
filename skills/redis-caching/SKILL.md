---
name: redis-caching
description: Redis & caching — data structures, cache invalidation, TTLs, eviction policy, stampede protection,
  distributed locks, pub/sub
triggers:
- redis
- cache
- caching
- cache invalidation
- ttl
- cache stampede
- memoization
version: 1.0.0
schema_version: 2
skill_id: redis-caching
task_families:
- backend-api
- maintenance
capabilities:
- performance
- configuration
- error-handling
aliases:
- redis
- caching
- cache
- ttl
- cache-invalidation
- cache-stampede
stack:
  languages: []
  frameworks:
  - redis
  independent: true
preconditions:
- Redis or a compatible cache is reachable from the service
- Cache keys, value shapes, and TTL policy are under your control
- A way to measure hit ratio and origin latency exists
required_tools:
- redis-cli
exclusions:
- Designing durable primary storage
- Cache is managed externally and cannot be changed
- SQL indexing or query tuning (use postgresql-pgvector)
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
- id: key-resources
  heading: Key Resources
  stage: inspect
  requires_sections: []
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
---

# Redis & Caching

## Core Skills

| Skill | Practice |
|---|---|
| Data structures | Strings, hashes, sorted sets, streams for the right shape |
| Key design | Namespaced, bounded keys with an explicit expiry |
| Invalidation | Write-through or delete-on-write, never both blindly |
| TTL strategy | Sliding vs absolute expiry chosen per access pattern |
| Eviction | Set maxmemory-policy deliberately; know what it drops |
| Stampede control | Lock or single-flight the recompute of a hot key |
| Distributed lock | SET NX PX plus a fencing token; never a bare SET |
| Pub/sub | Lightweight fan-out; use streams when durability matters |

## Pitfalls
- Caching without an expiry grows memory until eviction surprises you.
- A thundering herd on expiry hits the origin harder than no cache at all.
- Treating a cache as the source of truth loses data on eviction.
- Ignoring serialization cost can make the cache slower than the origin.

## Practice Project
Cache a hot read: choose a structure and TTL, add single-flight recompute, measure hit ratio and origin latency with and without the cache, and fail over cleanly when Redis is unavailable.

## Key Resources
- [Redis docs](https://redis.io/docs/latest/)
- [Redis eviction](https://redis.io/docs/latest/develop/reference/eviction/)
- [Cache stampede](https://en.wikipedia.org/wiki/Cache_stampede)
