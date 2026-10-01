---
name: security-sandboxing
description: "Security & sandboxing — sandboxing, PII redaction, rate limiting, input validation, audit logging, Row-Level Security"
triggers:
  - "security"
  - "sandbox"
  - "pii"
  - "rate limit"
  - "audit"
  - "rls"
version: 1.0.0
---

# Security & Sandboxing

## Core Skills

| Skill | Practice |
|---|---|
| Sandboxing | Docker-based sandbox for tool execution |
| PII redaction | Redact PII before LLM calls |
| Rate limiting | Token bucket rate limiter per session |
| Input validation | Validate all tool inputs against JSON Schema |
| Audit logging | Log all tool calls with correlation IDs |
| Row-Level Security | PostgreSQL RLS for per-user agent data |

## Key Resources
- [OWASP LLM Top 10](https://owasp.org/www-project-top-10-for-large-language-model-applications/)
- [Docker security](https://docs.docker.com/engine/security/)
- [PostgreSQL RLS](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)

## Practice Project
Build a sandboxed tool executor: agent proposes code → Docker container executes → result returned. Add PII redaction middleware.
