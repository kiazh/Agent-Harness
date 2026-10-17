---
name: security-sandboxing
description: Security & sandboxing — sandboxing, PII redaction, rate limiting, input validation, audit
  logging, Row-Level Security
triggers:
- security
- sandbox
- pii
- rate limit
- audit
- rls
version: 1.0.0
schema_version: 2
skill_id: security-sandboxing
task_families:
- security-review
- backend-api
capabilities:
- authentication
- database
- observability
aliases:
- sandboxing
- pii-redaction
- rate-limiting
- rls
- audit-logging
stack:
  languages:
  - python
  - sql
  frameworks:
  - docker
  - postgresql
  independent: false
preconditions:
- Tool or agent execution that needs isolation
- A data store holding user-scoped records
required_tools:
- docker
- shell
exclusions:
- Prompt-level defenses only (use prompt-injection-defense)
- Policy or compliance paperwork with no code change
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
