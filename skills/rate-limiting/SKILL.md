---
name: rate-limiting
description: "Rate limiting & throttling — token bucket, leaky bucket, sliding window, quotas, backpressure, retry-after, fair sharing"
triggers:
  - "rate limit"
  - "rate limiting"
  - "throttling"
  - "token bucket"
  - "quota"
  - "backpressure"
  - "leaky bucket"
version: 1.0.0
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
