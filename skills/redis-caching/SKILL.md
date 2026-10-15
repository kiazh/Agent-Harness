---
name: redis-caching
description: "Redis & caching — data structures, cache invalidation, TTLs, eviction policy, stampede protection, distributed locks, pub/sub"
triggers:
  - "redis"
  - "cache"
  - "caching"
  - "cache invalidation"
  - "ttl"
  - "cache stampede"
  - "memoization"
version: 1.0.0
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
